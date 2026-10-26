#!/usr/bin/env python3
"""
NodeScope crawler - discovers reachable Bitcoin nodes over the P2P network.

How it works
  1. Resolve the Bitcoin DNS seeds to get a starting set of node IPs.
  2. Connect to each node, perform the version/verack handshake (this tells us
     the node's software, protocol version, services and block height).
  3. Send `getaddr` and collect the peer addresses the node returns.
  4. Queue every new address and repeat until the time budget runs out.

Output
  data/latest.json   full snapshot of reachable nodes (deployed with the site)
  data/history.json  small time series [timestamp, nodes, height] (committed)

Only the Python standard library is required. If `maxminddb` is installed and
DB-IP Lite databases exist in ./geo, nodes are geolocated (country, city, ASN).
"""

import asyncio
import hashlib
import ipaddress
import json
import os
import random
import socket
import struct
import time
from collections import Counter
from pathlib import Path

# ---------------------------------------------------------------- settings
MAGIC = bytes.fromhex("f9beb4d9")          # Bitcoin mainnet
PROTOCOL_VERSION = 70016
USER_AGENT = b"/nodescope-crawler:0.1/"
DEFAULT_PORT = 8333

CRAWL_SECONDS = int(os.environ.get("CRAWL_SECONDS", "1200"))  # time budget
CONCURRENCY = int(os.environ.get("CONCURRENCY", "400"))      # parallel sockets
CONNECT_TIMEOUT = float(os.environ.get("CONNECT_TIMEOUT", "6"))
HANDSHAKE_TIMEOUT = 10
GETADDR_TIMEOUT = 12
MAX_ADDR_AGE = 3 * 86400        # ignore gossiped addresses older than 3 days
HISTORY_FULL_DAYS = 7           # keep every crawl for this many days...
HISTORY_BUCKET = 3 * 3600       # ...then one point per 3 hours, kept forever

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
GEO_DIR = ROOT / "geo"

DNS_SEEDS = [
    "seed.bitcoin.sipa.be",
    "dnsseed.bluematt.me",
    "dnsseed.bitcoin.dashjr-list-of-p2p-nodes.us",
    "seed.bitcoinstats.com",
    "seed.bitcoin.jonasschnelli.ch",
    "seed.btc.petertodd.net",
    "seed.bitcoin.sprovoost.nl",
    "dnsseed.emzy.de",
    "seed.bitcoin.wiz.biz",
    "seed.mainnet.achownodes.xyz",
]

ONIONCAT_PREFIX = bytes.fromhex("fd87d87eeb43")  # Tor v2 over addr v1 - skip


# ---------------------------------------------------------------- wire format
def sha256d(b: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def make_msg(command: str, payload: bytes = b"") -> bytes:
    return (MAGIC + command.encode().ljust(12, b"\0")
            + struct.pack("<I", len(payload)) + sha256d(payload)[:4] + payload)


def varint(n: int) -> bytes:
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + struct.pack("<H", n)
    if n <= 0xFFFFFFFF:
        return b"\xfe" + struct.pack("<I", n)
    return b"\xff" + struct.pack("<Q", n)


def read_varint(buf: bytes, off: int):
    first = buf[off]
    if first < 0xFD:
        return first, off + 1
    if first == 0xFD:
        return struct.unpack_from("<H", buf, off + 1)[0], off + 3
    if first == 0xFE:
        return struct.unpack_from("<I", buf, off + 1)[0], off + 5
    return struct.unpack_from("<Q", buf, off + 1)[0], off + 9


def ip_to_16(ip: str) -> bytes:
    addr = ipaddress.ip_address(ip)
    if addr.version == 4:
        return b"\0" * 10 + b"\xff\xff" + addr.packed
    return addr.packed


def ip_from_16(raw: bytes):
    """Return a printable IP, or None for onion/non-routable addresses."""
    if raw[:6] == ONIONCAT_PREFIX:
        return None
    if raw[:12] == b"\0" * 10 + b"\xff\xff":
        addr = ipaddress.IPv4Address(raw[12:])
    else:
        addr = ipaddress.IPv6Address(raw)
    return str(addr) if addr.is_global else None


def version_payload(ip: str, port: int) -> bytes:
    return (struct.pack("<iQq", PROTOCOL_VERSION, 0, int(time.time()))
            + struct.pack("<Q", 0) + ip_to_16(ip) + struct.pack(">H", port)  # addr_recv
            + struct.pack("<Q", 0) + b"\0" * 16 + struct.pack(">H", 0)       # addr_from
            + os.urandom(8)                                                  # nonce
            + varint(len(USER_AGENT)) + USER_AGENT
            + struct.pack("<i", 0)                                           # start_height
            + b"\x00")                                                       # relay = false


def parse_version(p: bytes) -> dict:
    version, services, _ts = struct.unpack_from("<iQq", p, 0)
    off = 20 + 26 + 26 + 8            # skip addr_recv, addr_from, nonce
    ua_len, off = read_varint(p, off)
    ua = p[off:off + ua_len].decode("utf-8", errors="replace")[:120]
    off += ua_len
    height = struct.unpack_from("<i", p, off)[0] if len(p) >= off + 4 else 0
    return {"protocol": version, "services": services, "ua": ua, "height": height}


def parse_addr(p: bytes):
    count, off = read_varint(p, 0)
    out = []
    for _ in range(min(count, 1000)):
        if off + 30 > len(p):
            break
        ts = struct.unpack_from("<I", p, off)[0]
        ip = ip_from_16(p[off + 12:off + 28])
        port = struct.unpack_from(">H", p, off + 28)[0]
        off += 30
        if ip and port:
            out.append((ts, ip, port))
    return out


async def read_msg(reader):
    header = await reader.readexactly(24)
    if header[:4] != MAGIC:
        raise ValueError("bad magic")
    command = header[4:16].rstrip(b"\0").decode("ascii", errors="replace")
    length = struct.unpack("<I", header[16:20])[0]
    if length > 4_000_000:
        raise ValueError("message too large")
    payload = await reader.readexactly(length)
    return command, payload


# ---------------------------------------------------------------- one node
async def visit(ip: str, port: int):
    """Handshake + getaddr. Returns (info, addresses) or None if unreachable."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), CONNECT_TIMEOUT)
    except Exception:
        return None
    try:
        writer.write(make_msg("version", version_payload(ip, port)))
        await writer.drain()

        state = {"info": None, "verack": False}

        async def handshake():
            while not (state["info"] and state["verack"]):
                cmd, payload = await read_msg(reader)
                if cmd == "version":
                    state["info"] = parse_version(payload)
                    writer.write(make_msg("verack"))
                    await writer.drain()
                elif cmd == "verack":
                    state["verack"] = True

        await asyncio.wait_for(handshake(), HANDSHAKE_TIMEOUT)

        writer.write(make_msg("getaddr"))
        await writer.drain()
        addresses = []

        async def collect():
            while True:
                cmd, payload = await read_msg(reader)
                if cmd == "ping":
                    writer.write(make_msg("pong", payload))
                    await writer.drain()
                elif cmd == "addr":
                    batch = parse_addr(payload)
                    addresses.extend(batch)
                    if len(batch) > 1:   # a real getaddr reply, not a self-announce
                        return

        try:
            await asyncio.wait_for(collect(), GETADDR_TIMEOUT)
        except Exception:
            pass  # node is still reachable even if it sends no addresses
        return state["info"], addresses
    except Exception:
        return None
    finally:
        writer.close()


# ---------------------------------------------------------------- crawl loop
async def resolve_seeds():
    loop = asyncio.get_running_loop()
    ips = set()
    for seed in DNS_SEEDS:
        try:
            infos = await asyncio.wait_for(
                loop.getaddrinfo(seed, DEFAULT_PORT, type=socket.SOCK_STREAM), 10)
            ips.update(info[4][0] for info in infos)
        except Exception as exc:
            print(f"  seed {seed}: {exc}")
    return sorted(ips)


def ipv6_available() -> bool:
    try:
        with socket.create_connection(("2001:4860:4860::8888", 53), timeout=3):
            return True
    except OSError:
        return False


async def crawl():
    deadline = time.time() + CRAWL_SECONDS
    use_ipv6 = ipv6_available()
    print(f"IPv6 connectivity: {'yes' if use_ipv6 else 'no (IPv6 nodes skipped)'}")

    queue: asyncio.Queue = asyncio.Queue()
    seen = set()
    reachable = {}
    active = 0

    def enqueue(ip, port):
        key = (ip, port)
        if key in seen:
            return
        if ":" in ip and not use_ipv6:
            return
        seen.add(key)
        queue.put_nowait(key)

    seeds = await resolve_seeds()
    random.shuffle(seeds)
    print(f"Resolved {len(seeds)} seed addresses")
    for ip in seeds:
        enqueue(ip, DEFAULT_PORT)

    async def worker():
        nonlocal active
        while time.time() < deadline:
            try:
                ip, port = await asyncio.wait_for(queue.get(), 5)
            except asyncio.TimeoutError:
                if active == 0 and queue.empty():
                    return
                continue
            active += 1
            try:
                result = await visit(ip, port)
            finally:
                active -= 1
            if not result:
                continue
            info, addresses = result
            reachable[(ip, port)] = info
            now = time.time()
            for ts, a_ip, a_port in addresses:
                if now - ts < MAX_ADDR_AGE:
                    enqueue(a_ip, a_port)

    async def progress():
        while time.time() < deadline:
            await asyncio.sleep(60)
            print(f"  {len(reachable):>6} reachable | {len(seen):>7} tried/queued "
                  f"| {queue.qsize():>7} waiting")

    reporter = asyncio.create_task(progress())
    try:
        await asyncio.wait_for(
            asyncio.gather(*(worker() for _ in range(CONCURRENCY))),
            CRAWL_SECONDS + 60)
    except asyncio.TimeoutError:
        pass
    reporter.cancel()
    return reachable, len(seen)


# ---------------------------------------------------------------- enrichment
def open_geo():
    try:
        import maxminddb
    except ImportError:
        print("maxminddb not installed - skipping geolocation")
        return None, None
    city = GEO_DIR / "dbip-city-lite.mmdb"
    asn = GEO_DIR / "dbip-asn-lite.mmdb"
    return (maxminddb.open_database(str(city)) if city.exists() else None,
            maxminddb.open_database(str(asn)) if asn.exists() else None)


def consensus_height(heights):
    """Pick the network's block height from what nodes reported at handshake.

    Nodes report their height at the moment we connected, and a crawl lasts
    10-20 minutes (1-2 new blocks), so honest values cluster a few blocks
    apart. Some fake/spy nodes all report the same bogus height, so we must
    not trust the highest values. Strategy: slide a 3-block window over the
    reported heights and pick the window holding the most nodes; its top
    end is the consensus height. A minority cluster can never win.
    """
    counts = Counter(h for h in heights if h > 0)
    if not counts:
        return 0
    def support(h):
        return counts[h] + counts[h - 1] + counts[h - 2]
    return max(counts, key=lambda h: (support(h), h))


def build_rows(reachable):
    city_db, asn_db = open_geo()
    rows = []
    for (ip, port), info in reachable.items():
        cc = country = city = org = ""
        lat = lon = None
        asn = 0
        if city_db:
            rec = city_db.get(ip) or {}
            cc = (rec.get("country") or {}).get("iso_code", "")
            country = ((rec.get("country") or {}).get("names") or {}).get("en", "")
            city = ((rec.get("city") or {}).get("names") or {}).get("en", "")
            loc = rec.get("location") or {}
            if "latitude" in loc:
                lat, lon = round(loc["latitude"], 2), round(loc["longitude"], 2)
        if asn_db:
            rec = asn_db.get(ip) or {}
            asn = rec.get("autonomous_system_number", 0) or 0
            org = rec.get("autonomous_system_organization", "") or ""
        rows.append([ip, port, info["ua"], info["protocol"], info["services"],
                     info["height"], cc, country, city, lat, lon, asn, org])
    rows.sort(key=lambda r: (r[6], r[0]))
    return rows


def point_time(point):
    return point[0] if isinstance(point, list) else point.get("t", 0)


def merge_points(*lists):
    """Union of history lists, one point per timestamp, oldest first."""
    by_time = {}
    for points in lists:
        for p in points:
            t = point_time(p)
            if t and (t not in by_time or isinstance(p, dict)):  # prefer the richer format
                by_time[t] = p
    return [by_time[t] for t in sorted(by_time)]


def parse_history(text):
    """Parse history.json, recovering from a Git merge conflict instead of losing data.

    A conflicted file looks like:  <<<<<<< HEAD / [..] / ======= / [..] / >>>>>>> abc
    Both sides are valid JSON lines, so we keep every point from both."""
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        pass
    sides = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            try:
                sides.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    if sides:
        print(f"  history.json had a merge conflict - recovered {len(sides)} versions")
    return merge_points(*sides)


def read_history(path):
    try:
        return parse_history(Path(path).read_text())
    except FileNotFoundError:
        return []


def merge_history_files(theirs, mine, out):
    """Used by the workflow: combine the repo's newest history with this run's."""
    merged = compact_history(merge_points(read_history(theirs), read_history(mine)), int(time.time()))
    Path(out).write_text(json.dumps(merged, separators=(",", ":")))
    print(f"Merged history: {len(merged)} points")


def compact_history(history, now):
    """Keep every point from the last week; older points thin out to one per
    3-hour bucket. Nothing is ever dropped completely, so long-term trends survive
    while the file stays small (~1 MB per year at 15-minute crawls)."""
    cutoff = now - HISTORY_FULL_DAYS * 86400
    kept, seen_buckets = [], set()
    for point in history:
        t = point[0] if isinstance(point, list) else point["t"]
        if t >= cutoff:
            kept.append(point)
            continue
        bucket = t // HISTORY_BUCKET
        if bucket not in seen_buckets:
            seen_buckets.add(bucket)
            kept.append(point)
    return kept


def fetch_chain_tip() -> int:
    """Current block height from public explorers (0 if none answer)."""
    import urllib.request
    for url in ("https://mempool.space/api/blocks/tip/height",
                "https://blockstream.info/api/blocks/tip/height"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nodescope-crawler"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return int(resp.read().strip())
        except Exception as exc:
            print(f"  tip from {url}: {exc}")
    return 0


# ---------------------------------------------------------------- main
def main():
    started = time.time()
    print(f"Crawling for up to {CRAWL_SECONDS}s with {CONCURRENCY} connections")
    reachable, attempted = asyncio.run(crawl())
    rows = build_rows(reachable)
    consensus = consensus_height([r[5] for r in rows])
    tip = fetch_chain_tip()
    # Trust an independent explorer when it roughly agrees with the nodes;
    # otherwise fall back to what the majority of nodes reported.
    height = tip if tip and abs(tip - consensus) < 2000 else consensus
    in_sync = sum(1 for r in rows if height - 6 <= r[5] <= height + 2)
    knots = sum(1 for r in rows if "knots" in r[2].lower())
    countries = dict(Counter(r[6] for r in rows if r[6]).most_common(25))
    now = int(time.time())

    snapshot = {
        "timestamp": now,
        "duration": round(now - started),
        "attempted": attempted,
        "total": len(rows),
        "height": height,
        "consensus": consensus,
        "tip": tip,
        "in_sync": in_sync,
        "knots": knots,
        "columns": ["ip", "port", "user_agent", "protocol", "services", "height",
                    "country_code", "country", "city", "lat", "lon", "asn", "org"],
        "nodes": rows,
    }
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "latest.json").write_text(json.dumps(snapshot, separators=(",", ":")))

    history_file = DATA_DIR / "history.json"
    history = read_history(history_file)
    if rows:  # never record an empty crawl (e.g. network failure) as "0 nodes"
        # t=time, n=nodes, h=height, s=in sync, k=Knots nodes, f=full nodes,
        # c=number of countries, cc=nodes per country (top 25)
        full = sum(1 for r in rows if r[4] & 1)
        history.append({"t": now, "n": len(rows), "h": height, "s": in_sync, "k": knots,
                        "f": full, "c": len({r[6] for r in rows if r[6]}), "cc": countries})
    history = compact_history(history, now)
    history_file.write_text(json.dumps(history, separators=(",", ":")))

    print(f"Chain tip from explorer: {tip or 'unavailable'} | node consensus: {consensus} "
          f"| in sync: {in_sync}/{len(rows)}")
    print(f"Done: {len(rows)} reachable nodes, height {height}, "
          f"{attempted} addresses tried, {now - started:.0f}s")


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 5 and sys.argv[1] == "--merge-history":
        merge_history_files(*sys.argv[2:])
    else:
        main()

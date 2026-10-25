"""Offline test: a fake Bitcoin peer on localhost speaking the real wire format.

Run:  python crawler/test_crawl.py
"""
import asyncio
import struct
import time

import crawl as c


def fake_version(height=900_000, ua=b"/Satoshi:30.0.0/"):
    return (struct.pack("<iQq", 70016, 1033, int(time.time()))
            + b"\0" * 26 + b"\0" * 26 + b"\0" * 8
            + c.varint(len(ua)) + ua + struct.pack("<i", height) + b"\x01")


def fake_addr(entries):
    body = c.varint(len(entries))
    for ip, port, ts in entries:
        body += struct.pack("<IQ", ts, 1) + c.ip_to_16(ip) + struct.pack(">H", port)
    return body


async def handle(reader, writer):
    await c.read_msg(reader)                                   # their version
    writer.write(c.make_msg("version", fake_version()))
    writer.write(c.make_msg("verack"))
    await writer.drain()
    while True:
        cmd, _ = await c.read_msg(reader)
        if cmd == "getaddr":
            now = int(time.time())
            writer.write(c.make_msg("addr", fake_addr([("8.8.8.8", 8333, now)])))  # self-announce
            writer.write(c.make_msg("ping", b"12345678"))
            writer.write(c.make_msg("addr", fake_addr([
                ("1.2.3.4", 8333, now),
                ("2a01:4f8::1", 8333, now),
                ("10.0.0.1", 8333, now),          # private -> dropped
                ("9.9.9.9", 18444, now - 10 * 86400),
            ])))
            await writer.drain()
            break
    await asyncio.sleep(0.2)
    writer.close()


async def main():
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    info, addrs = await c.visit("127.0.0.1", port)
    server.close()

    assert info == {"protocol": 70016, "services": 1033,
                    "ua": "/Satoshi:30.0.0/", "height": 900_000}, info
    ips = [a[1] for a in addrs]
    assert ips == ["8.8.8.8", "1.2.3.4", "2a01:4f8::1", "9.9.9.9"], ips
    assert await c.visit("127.0.0.1", 1) is None                 # closed port
    assert c.consensus_height([0, 5, 899_990, 900_001, 900_001, 900_002, 10**9]) in (900_001, 900_002)
    print("all crawler tests passed")


asyncio.run(main())

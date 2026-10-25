# NodeScope — Bitcoin Node Tracker

A free, self-updating dashboard of reachable Bitcoin nodes, hosted on GitHub Pages.

- **Live map** of nodes worldwide
- **Stats**: reachable nodes, consensus block height, countries, full vs pruned, in-sync %
- **Top user agents** (Bitcoin Core / Knots versions), **top countries**, **top networks (ASN)**
- **History chart** of node count over time
- **Searchable table** of every node, with CSV download

Everything runs for free on GitHub: a scheduled GitHub Action crawls the Bitcoin
network every 3 hours and republishes the page.

---

## Setup (about 5 minutes, no coding)

### 1. Create the repository
1. Sign in to GitHub and click **New repository** (top-right **+** menu).
2. Name it, for example `nodescope`. Choose **Public** (Actions are unlimited and free for public repos).
3. Click **Create repository**.

### 2. Upload the files
1. On the new repo page click **uploading an existing file**.
2. Unzip `nodescope.zip` on your computer and drag **everything inside the `nodescope` folder** into the browser
   (`index.html`, `README.md`, `.gitignore`, and the `crawler`, `data`, `.github` folders).
   > On Mac, press `Cmd + Shift + .` in Finder to show hidden items like `.github`.
3. Click **Commit changes**.

Check that the file `.github/workflows/crawl.yml` appears in the repo. Without it nothing runs.

### 3. Turn on GitHub Pages
1. Go to **Settings → Pages**.
2. Under **Build and deployment → Source**, choose **GitHub Actions**.

### 4. Run the first crawl
1. Go to the **Actions** tab. If asked, click **I understand my workflows, go ahead and enable them**.
2. Click **Crawl Bitcoin nodes** → **Run workflow** → **Run workflow**.
3. Wait about 25 minutes until it shows a green check.

### 5. Open your dashboard
Your site is at:

```
https://YOUR-GITHUB-USERNAME.github.io/nodescope/
```

(The link is also shown in **Settings → Pages** and on the finished workflow run.)
From now on it updates automatically every 3 hours.

---

## Customizing

| What | Where |
| --- | --- |
| Site name / tagline | `index.html` → `<title>` and the `<h1>` / `<p>` in the header |
| Accent color | `index.html` → `--accent` in the `:root` CSS block |
| How often it crawls | `.github/workflows/crawl.yml` → `cron` (e.g. `"17 * * * *"` = hourly) |
| Crawl length | `.github/workflows/crawl.yml` → `CRAWL_SECONDS` (longer = more nodes found) |

## Good to know

- **Numbers differ from other trackers.** Each tracker counts differently. This one counts nodes that
  accepted a connection during a 20-minute crawl over IPv4 (and IPv6 when the runner supports it).
  Tor-only nodes are not included, so totals are lower than sites that count Tor.
- **Private repos** get limited free Action minutes (about 2,000/month). Each crawl uses ~25 minutes, so
  either keep the repo public or change the schedule to every 12 hours.
- **GitHub pauses schedules after 60 days without commits.** The workflow commits `data/history.json`
  after every crawl, which keeps it active.
- If the first run fails at the **deploy** step, Pages wasn't set to *GitHub Actions* yet. Fix step 3,
  then click **Re-run all jobs**.

## How it works

```
GitHub Actions (every 3 h)
  └─ crawler/crawl.py
       1. resolve Bitcoin DNS seeds
       2. connect to each node: version → verack  (gets software, height, services)
       3. getaddr → learn more peers → repeat
       4. geolocate with DB-IP Lite
       5. write data/latest.json + data/history.json
  └─ publish index.html + data/ to GitHub Pages
```

Run the crawler on your own computer (Python 3.9+):

```bash
pip install maxminddb        # optional, for locations
CRAWL_SECONDS=300 python crawler/crawl.py
python -m http.server 8000   # then open http://localhost:8000
```

Offline protocol test: `cd crawler && python test_crawl.py`

## Credits

IP geolocation by [DB-IP](https://db-ip.com) (CC BY 4.0). Map tiles © OpenStreetMap contributors © CARTO.
Map library: [Leaflet](https://leafletjs.com).

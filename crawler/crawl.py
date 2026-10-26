name: Crawl Bitcoin nodes

on:
  schedule:
    - cron: "7,22,37,52 * * * *"   # every 15 minutes (GitHub may delay busy-hour runs a little)
  workflow_dispatch:            # "Run workflow" button in the Actions tab
  push:
    branches: [main]
    paths: ["index.html", "crawler/**", ".github/workflows/**"]

permissions:
  contents: write    # commit data/history.json
  pages: write       # deploy the site
  id-token: write

concurrency:
  group: crawl
  cancel-in-progress: false

jobs:
  crawl-and-deploy:
    runs-on: ubuntu-latest
    timeout-minutes: 25
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install geolocation reader
        run: pip install maxminddb

      - name: Download DB-IP Lite geolocation databases (free, CC BY 4.0)
        continue-on-error: true
        run: |
          mkdir -p geo
          fetch() {
            for M in $(date -u +%Y-%m) $(date -u -d "$(date -u +%Y-%m-15) -1 month" +%Y-%m); do
              if curl -fsSL "https://download.db-ip.com/free/$1-$M.mmdb.gz" -o "geo/$1.mmdb.gz"; then
                gunzip -f "geo/$1.mmdb.gz"; echo "Got $1 ($M)"; return 0
              fi
            done
            echo "::warning::Could not download $1 - map locations will be missing"
          }
          fetch dbip-city-lite
          fetch dbip-asn-lite

      - name: Crawl the Bitcoin network
        env:
          CRAWL_SECONDS: "660"    # 11 minutes, so a run finishes before the next starts
          CONCURRENCY: "400"
        run: python crawler/crawl.py

      - name: Save history (merge with newest repo copy, retry if another run pushed first)
        run: bash crawler/save_history.sh

      - name: Build site
        run: |
          python -c "import json; json.load(open('data/history.json'))"   # never publish a broken file
          mkdir -p _site/data
          cp index.html _site/
          cp data/latest.json data/history.json _site/data/

      - uses: actions/configure-pages@v5
      - uses: actions/upload-pages-artifact@v3
        with:
          path: _site
      - id: deployment
        uses: actions/deploy-pages@v4

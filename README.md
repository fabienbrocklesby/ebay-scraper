# eBay Scraper

Distributed eBay product scraper. Scrapes full product data from eBay seller store URLs at scale and outputs to CSV.

**Architecture:** one coordinator machine runs Redis (job queue) and Postgres (results). Any number of worker VPS nodes connect to it, pull jobs from the queue, scrape eBay, and write results. Adding workers increases throughput linearly with no code changes.

---

## Install

```bash
pipx install git+https://github.com/OWNER/ebay-scraper
```

Installs the `scraper` command globally. Works on macOS and Linux. Requires Python 3.11+ and pipx.

To update:

```bash
pipx reinstall ebay-scraper
```

---

## Network Setup (Tailscale, recommended)

Tailscale creates an encrypted private network between your machines so Redis and Postgres are never exposed to the public internet.

1. Install Tailscale on each machine: https://tailscale.com/download
2. Run `tailscale up` on each machine to join your tailnet
3. Use the Tailscale hostname (e.g. `mycoordinator.tail1234.ts.net`) when connecting workers

Skip this step only for single-machine or LAN setups.

---

## Coordinator Setup

Run once on the machine that will host Redis and Postgres (requires Docker):

```bash
scraper coordinator start
scraper connect localhost
scraper db init
```

`coordinator start` launches Redis and Postgres as Docker containers with `restart: unless-stopped`.

To check status or stop:

```bash
scraper coordinator status
scraper coordinator stop
```

---

## Adding Worker VPS Nodes

On each worker VPS:

**Option 1: Docker worker (recommended)**

```bash
# Build the worker image (clone repo first, or copy Dockerfile)
git clone https://github.com/OWNER/ebay-scraper
cd ebay-scraper
docker build -t ebay-scraper-worker .

# Connect to coordinator, then print the docker run command
scraper connect <coordinator-tailscale-hostname>
scraper worker docker-run
```

Copy the printed `docker run` command and run it. It includes `--restart=always` so the worker survives reboots.

**Option 2: Direct Python (if Python 3.11+ is available)**

```bash
pipx install git+https://github.com/OWNER/ebay-scraper
scraper connect <coordinator-tailscale-hostname>
scraper worker start
```

---

## Usage

### Add stores to the queue

```bash
# Single store
scraper add https://www.ebay.com/str/sellername --niche car-accessories

# Australian eBay
scraper add https://www.ebay.com.au/str/sellername --niche car-accessories

# File of store URLs (one per line)
scraper add stores.txt --niche car-accessories
```

The `--niche` tag groups items for filtered exports. The command crawls the store, skips already-queued items, and adds the rest to the Redis queue. Workers pick them up automatically.

### Check progress

```bash
scraper status
```

Shows jobs waiting in queue, total products scraped, and a per-niche breakdown.

### Export to CSV

```bash
# All products
scraper export --output all-products.csv

# Single niche
scraper export --niche car-accessories --output car.csv
```

CSV columns: `item_id, title, price, currency, condition, description, image_urls, item_url, seller_id, store_url, category, item_specifics, mpn, upc, shipping, listing_type, niche, scraped_at`

- `image_urls`: pipe-separated list of image URLs (no downloading)
- `item_specifics`: JSON of all eBay item specifics (brand, model, colour, etc.)
- `mpn`: Manufacturer Part Number extracted from item specifics
- `upc`: UPC/EAN barcode extracted from item specifics

### Clear a niche

```bash
scraper clear --niche car-accessories
```

---

## Configuration

Config is stored at `~/.config/ebay-scraper/.env` (written by `scraper connect`). You can also set these as environment variables, which take precedence.

| Variable | Required | Description |
|---|---|---|
| `REDIS_URL` | Yes | e.g. `redis://coordinator.ts.net:6379` |
| `DATABASE_URL` | Yes | e.g. `postgresql://scraper:scraper@coordinator.ts.net:5432/ebayscraper` |
| `PROXY_URL` | No | Rotating residential proxy: `http://user:pass@host:port` |
| `REQUESTS_PER_SECOND` | No | Rate per worker (default: 0.5) |

A residential proxy is required for VPS workers to scrape eBay item pages. Datacenter IPs receive JS challenge pages rather than product data. Set `PROXY_URL` before starting workers on a VPS.

---

## Scale Reference

| Workers | Items/day at 0.5 req/s |
|---|---|
| 1 | ~43,000 |
| 5 | ~216,000 |
| 10 | ~432,000 |
| 25 | ~1,080,000 |

---

## Running Tests (development)

```bash
docker compose up -d
docker exec $(docker ps -qf "ancestor=postgres:16-alpine") psql -U scraper -c "CREATE DATABASE ebayscraper_test;" 2>/dev/null || true
pytest -v
```

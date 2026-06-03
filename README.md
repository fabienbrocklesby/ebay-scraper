# eBay Scraper

Distributed eBay product scraper. Scrapes full product data from eBay seller store URLs at scale (1M+ products/day across multiple VPS nodes) and outputs to CSV.

**Architecture:** one coordinator VPS runs Redis (job queue) and Postgres (results). Any number of worker VPS nodes pull item IDs from the queue, scrape eBay, and write results to Postgres. Adding more workers increases throughput linearly, with no code changes.

---

## Requirements

- Python 3.11+
- Docker (coordinator only, for Redis and Postgres)

---

## Coordinator Setup

Run once on the coordinator VPS:

```bash
git clone <repo-url> ebay-scraper
cd ebay-scraper

pip install -e .

cp .env.example .env
# Edit .env: set DATABASE_URL, REDIS_URL, and optionally PROXY_URL

docker compose up -d    # starts Redis and Postgres
scraper db init         # creates the products table
```

---

## Worker Setup

Run on each worker VPS:

```bash
git clone <repo-url> ebay-scraper
cd ebay-scraper

pip install -e .

cp .env.example .env
# Edit .env: point REDIS_URL and DATABASE_URL at the coordinator VPS IP

scraper worker start    # runs until Ctrl+C
```

Add more workers at any time by repeating this on additional VPS nodes.

---

## Usage

### Add a store to the queue

```bash
# Single store URL
scraper add https://www.ebay.com/str/sellername --niche car-accessories

# File of store URLs (one per line)
scraper add stores.txt --niche car-accessories
```

The `--niche` tag groups items for filtered exports later. The command fetches all item IDs from the store, skips any already queued or scraped, and adds the rest to the Redis job queue.

### Check queue and scrape progress

```bash
scraper status
```

Shows jobs waiting in queue, total products scraped, and a per-niche breakdown.

### Export to CSV

```bash
# All products
scraper export --output all-products.csv

# Single niche
scraper export --niche car-accessories --output car-accessories.csv
```

CSV columns: `item_id, title, price, currency, condition, description, image_urls, item_url, seller_id, store_url, category, item_specifics, mpn, upc, shipping, listing_type, niche, scraped_at`

- `image_urls`: pipe-separated list of image URLs
- `item_specifics`: JSON object of all eBay item specifics (brand, model, colour, etc.)
- `mpn`: Manufacturer Part Number, extracted from item specifics
- `upc`: UPC/EAN barcode, extracted from item specifics

### Clear a niche

```bash
scraper clear --niche car-accessories
```

Removes all scraped records for that niche. Prompts for confirmation.

---

## Configuration

All settings are read from `.env` in the project root (or environment variables).

| Variable | Required | Description |
|---|---|---|
| `REDIS_URL` | Yes | Redis connection string, e.g. `redis://coordinator-ip:6379` |
| `DATABASE_URL` | Yes | Postgres DSN, e.g. `postgresql://scraper:scraper@coordinator-ip/ebayscraper` |
| `PROXY_URL` | No | Rotating residential proxy, format `http://user:pass@host:port` |
| `REQUESTS_PER_SECOND` | No | Rate limit per worker (default: 0.5) |

---

## Scale Reference

| Workers | Items/day (at 0.5 req/s) |
|---|---|
| 1 | ~43,000 |
| 5 | ~216,000 |
| 10 | ~432,000 |
| 25 | ~1,080,000 |

Throughput scales linearly. Increase `REQUESTS_PER_SECOND` with a residential proxy to push higher per-worker rates.

---

## Running Tests

Requires Docker running:

```bash
docker compose up -d
docker exec $(docker ps -qf "ancestor=postgres:16-alpine") psql -U scraper -c "CREATE DATABASE ebayscraper_test;" 2>/dev/null || true
pytest -v
```

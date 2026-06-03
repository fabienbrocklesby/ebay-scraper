# eBay Scraper: Design Spec

**Date:** 2026-06-03
**Client:** Kieran Granger
**Goal:** Scrape full product data from eBay seller stores at scale (1M products/day), output to CSV, deployable across multiple VPS nodes.

---

## Architecture

```
[Coordinator VPS]
  Redis       - job queue (item IDs waiting to be scraped)
  Postgres    - scraped results storage
  CLI         - manages queues, exports, status

[Worker VPS x N]
  scraper worker - pulls item IDs from Redis, scrapes eBay, writes to Postgres
```

**Flow:**
1. `scraper add <store-url> --niche <tag>` paginates through all store listings and pushes item IDs into Redis
2. Workers on each VPS pull item IDs from the queue, scrape the detail page, write to Postgres
3. `scraper export` pulls results from Postgres into a CSV at any time

Workers are stateless. Add or remove VPS nodes freely. If a worker crashes, the item ID returns to the queue automatically via Redis job timeout. Item IDs are deduplicated before queuing so no item is scraped twice.

---

## Data Schema

Each scraped product record contains:

| Field | Notes |
|---|---|
| `item_id` | eBay item ID (primary key) |
| `title` | Full listing title |
| `price` | Numeric |
| `currency` | USD, AUD, etc. |
| `condition` | New / Used / Refurbished |
| `description` | Full HTML description |
| `image_urls` | Pipe-separated list of all image URLs |
| `item_url` | Full eBay listing URL |
| `seller_id` | eBay seller username |
| `store_url` | Store URL this item was scraped from |
| `category` | eBay category name |
| `item_specifics` | JSON key-value pairs (brand, model, size, colour, etc.) |
| `mpn` | Manufacturer Part Number (from item specifics) |
| `upc` | UPC or EAN barcode (from item specifics) |
| `shipping` | Free / cost amount |
| `listing_type` | Buy It Now / Auction |
| `scraped_at` | Timestamp |
| `niche` | Tag set at add-time (e.g. `car-accessories`) |

`item_specifics` is stored as JSON to handle varying attributes across categories without breaking the CSV structure.

---

## CLI Interface

```bash
# Add stores to the queue
scraper add https://www.ebay.com/str/somestore --niche car-accessories
scraper add stores.txt --niche car-accessories   # bulk: one URL per line

# Start a worker (runs until stopped)
scraper worker start

# Export to CSV
scraper export --output products.csv
scraper export --niche car-accessories --output car-products.csv

# Check status
scraper status
# Shows: queued jobs, active workers, total scraped, per-niche counts

# Clear scraped data and re-queue (removes DB records + re-enqueues store)
scraper clear --niche car-accessories
```

**Config via `.env` on each machine:**

```
REDIS_URL=redis://coordinator-ip:6379
DATABASE_URL=postgresql://user:pass@coordinator-ip/ebayscraper
PROXY_URL=http://user:pass@proxy-host:port   # optional
REQUESTS_PER_SECOND=0.5                      # rate limit per worker
```

---

## Tech Stack

- Python 3.11+
- `httpx` + `asyncio` - async HTTP scraping
- `redis` + `rq` - job queue
- `asyncpg` - Postgres async driver
- `click` - CLI
- `beautifulsoup4` - HTML parsing
- `python-dotenv` - config loading

No heavy frameworks. Installs with `pip install -r requirements.txt`.

---

## Deployment

**Coordinator (one-time setup, run on coordinator VPS only):**
```bash
docker compose up -d   # starts Redis + Postgres
scraper db init        # creates schema
```

**New worker VPS:**
```bash
git clone <repo>
pip install -r requirements.txt
cp .env.example .env   # fill in REDIS_URL and DATABASE_URL
scraper worker start
```

A Docker Compose file is included for the coordinator services.

---

## Scale & Rate Limiting

- Realistic per-worker throughput with rotating proxies: 50,000-200,000 items/day
- To hit 1M/day: 5-10 worker VPS nodes in parallel
- Rate limiting is configurable via `REQUESTS_PER_SECOND` in `.env`
- Proxy support via `PROXY_URL` (standard HTTP proxy format, compatible with rotating residential proxy providers)
- Retry with exponential backoff on failed requests
- Workers report block rate; if it exceeds ~5%, proxy rotation needs tuning

**Note:** Proxy costs at this scale run $500-2000+/month - this is on Kieran's infrastructure budget, not the build cost.

---

## Reusability

New niches require zero code changes. Add stores with a new `--niche` tag and export by niche. Each deployment is config-only: update `.env` with the coordinator address, run the worker.

---

## Out of Scope

- Web UI or dashboard
- Image downloading (URLs only)
- Authenticated eBay pages
- Automatic proxy rotation management (Kieran provides proxy URL)

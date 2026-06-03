# eBay Scraper - Agent Instructions

## What This Is

A distributed eBay product scraper built for Kieran Granger. Scrapes full product data from eBay seller store URLs at scale (1M+ products/day across multiple VPS nodes), outputs to CSV. $500 NZD project.

## Status

**Not yet implemented.** Spec and implementation plan are written. No source code exists yet. Start from Task 1 of the plan.

## Implementation Plan

`docs/superpowers/plans/2026-06-03-ebay-scraper.md`

Read this first. It contains every task with complete code for every step. Execute it task by task using the `superpowers:subagent-driven-development` or `superpowers:executing-plans` skill.

## Design Spec

`docs/superpowers/specs/2026-06-03-ebay-scraper-design.md`

Reference for architecture decisions.

## Architecture Summary

- **Coordinator VPS**: runs Redis (rq job queue) + Postgres (results)
- **Worker VPS nodes** (as many as needed): pull item IDs from Redis, scrape eBay, write to Postgres
- **CLI**: `scraper add <store-url> --niche <tag>` / `scraper worker start` / `scraper export`
- Horizontal scaling: add more VPS workers, no code changes needed

## Tech Stack

- Python 3.11+
- `httpx` - async HTTP scraping
- `beautifulsoup4` - HTML parsing
- `rq` - Redis job queue
- `psycopg2-binary` - sync DB writes in workers
- `asyncpg` - async DB reads in CLI (export, status)
- `click` - CLI
- `python-dotenv` - config

## File Structure to Create

```
scraper/
├── __init__.py
├── cli.py       - Click CLI entry point
├── config.py    - Settings from .env
├── db.py        - Postgres schema + async CRUD (asyncpg)
├── queue.py     - rq queue helpers + deduplication
├── store.py     - eBay store paginator (extracts item IDs)
├── scraper.py   - eBay item detail page parser (returns ProductData)
├── worker.py    - rq job: scrape_and_store (sync, psycopg2) + start_worker
└── export.py    - CSV export
tests/
├── conftest.py
├── test_config.py
├── test_db.py
├── test_queue.py
├── test_store.py
├── test_scraper.py
├── test_worker.py
├── test_export.py
└── test_integration.py
docker-compose.yml  (Redis + Postgres for coordinator)
.env.example
pyproject.toml      (includes `scraper` CLI entry point)
requirements.txt
README.md
```

## Data Schema (CSV columns)

`item_id, title, price, currency, condition, description, image_urls, item_url, seller_id, store_url, category, item_specifics, mpn, upc, shipping, listing_type, niche, scraped_at`

- `image_urls`: pipe-separated list of image URLs (no downloading)
- `item_specifics`: JSON object of all eBay item specifics (brand, model, colour, etc.)
- `mpn`: Manufacturer Part Number extracted from item_specifics
- `upc`: UPC/EAN barcode extracted from item_specifics
- `niche`: tag set at scrape time, used to filter exports

## Key Implementation Notes

- Workers use **psycopg2 (sync)** not asyncpg. This is intentional: rq runs sync functions, using asyncpg inside rq workers causes event loop conflicts.
- CLI commands that read/export use **asyncpg (async)** wrapped in `asyncio.run()`.
- Deduplication: a Redis set (`scraped_items`) tracks queued item IDs. `scraper add` skips IDs already in the set.
- Scraper parses **JSON-LD** (`<script type="application/ld+json">`) from eBay item pages - more stable than CSS selectors.
- `mpn` and `upc` are extracted from item_specifics JSON after parsing - look for keys "MPN", "Manufacturer Part Number", "UPC", "EAN".
- Proxy: set `PROXY_URL` in `.env`, format `http://user:pass@host:port`. Workers pass it to httpx as proxies dict.

## Config (.env)

```
REDIS_URL=redis://coordinator-ip:6379
DATABASE_URL=postgresql://scraper:scraper@coordinator-ip/ebayscraper
PROXY_URL=          # optional, rotating residential proxy
REQUESTS_PER_SECOND=0.5
```

## Running Tests

Requires Docker running with coordinator services:
```bash
docker compose up -d
docker exec $(docker ps -qf "ancestor=postgres:16-alpine") psql -U scraper -c "CREATE DATABASE ebayscraper_test;" 2>/dev/null || true
pytest -v
```

## TDD Approach

The plan uses TDD. For each module: write the failing test first, run it to confirm failure, then implement the minimum code to make it pass. Do not skip this - the tests catch real bugs in the scraper selectors and SQL.

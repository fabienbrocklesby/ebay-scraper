# eBay Scraper - Agent Instructions

## What This Is

A distributed eBay product scraper built for Kieran Granger. Scrapes full product data from eBay seller store URLs at scale (1M+ products/day across multiple VPS nodes), outputs to CSV. $500 NZD project. This tool will run in production for years with zero maintenance.

## Status

**Implemented and validated against live eBay, including a real VPS + coordinator + proxy run.** All modules in `scraper/` exist with passing tests (`pytest`, 78 tests). The full chain (coordinator services, store add, scrape start, batched worker, delta, export) has been run end to end on real infra. See `README.md` for the user-facing setup and usage guide.

The throughput/cost redesign is captured in `docs/superpowers/specs/2026-06-04-ebay-scraper-scale-redesign.md` and `docs/superpowers/plans/2026-06-04-ebay-scraper-scale-redesign.md`. What changed from the original per-item design:

- **Batched concurrent worker.** The queue job is now `scrape_batch` (a batch of ~200 item URLs fetched concurrently with a thread pool, then bulk-upserted), not one item per job. One rq worker process per box; a per-IP `TokenBucket` (`scraper/throttle.py`) is the only rate limiter. Scale by adding boxes. Knobs: `WORKER_CONCURRENCY`, `MAX_RPS_PER_IP`, `BATCH_SIZE`.
- **Delta mode.** `scraper scrape delta` re-reads cheap listing pages and only re-fetches new or price-changed items (`scraper/delta.py`, `compute_delta`). Backfill once with `scrape start`, then delta on a schedule.
- **Topology decides cost (measured live).** A **residential-IP** worker fetches eBay directly for free (cheap path, millions/day via more boxes / higher `MAX_RPS_PER_IP`). A **datacenter/VPS** IP gets a hard **403** and must fetch through the residential proxy, paying bandwidth (~0.8-1 MB/item). The worker tries its own IP first and escalates to the proxy on a block (`BoxProxyState`), so one build runs both ways.

Key hard-won behaviour to preserve:

- eBay serves bot-challenge pages with **HTTP 200** (titles "Security Measure" and "Pardon Our Interruption"), detected in `scraper/fetch.py` (`is_challenge_page`); a **403** is also a block. Neither may be read as "no more data": that silently truncates crawls and drops items. Store crawls raise `ChallengeError` (loud, with a partial count); item fetches raise so the batch requeues/escalates.
- **A cold session gets 403 even from a residential IP.** `scrape_item`'s own-client path warms via a homepage GET; the batched worker injects a **warmed, reused** per-thread session (`_warmed_session`) to skip the per-item warmup+sleep while still carrying cookies. Do not "optimize" the warmup away.
- On macOS, rq's per-job fork aborts under Objective-C fork-safety; `start_worker` sets `OBJC_DISABLE_INITIALIZE_FORK_SAFETY` on darwin. Production workers run on Linux (Docker) and are unaffected.

## Implementation Plan

`docs/superpowers/plans/2026-06-03-ebay-scraper.md`

Read this fully before writing any code. It contains every task with complete code for every step. Execute it task by task.

## Design Spec

`docs/superpowers/specs/2026-06-03-ebay-scraper-design.md`

Reference for architecture decisions. Do not deviate from this without a strong reason.

---

## Engineering Standards (non-negotiable)

This tool will run unsupervised in production for years. There is no maintenance plan. Every decision must be made with that in mind.

### Code Quality

- **Senior engineer standard throughout.** Every file should be readable and maintainable by a Python developer who has never seen this codebase.
- **Minimal dependencies.** Every dependency is a future maintenance burden. Only add a package if the stdlib genuinely cannot do the job. Do not add convenience wrappers around things Python already does well.
- **Right tool for the right job.** Do not use async where sync is cleaner and sufficient. Do not use a heavy framework where a simple function works. The plan's sync/async boundary is deliberate - respect it.
- **Modular, single-responsibility files.** Each file in `scraper/` does one thing. If a file starts doing two things, it needs to be split.
- **No tech debt.** Do not leave TODOs, commented-out code, half-finished error handling, or "good enough for now" shortcuts. If something is worth doing, do it properly or flag it explicitly.
- **Clean, self-documenting code.** Well-named functions and variables are the documentation. Do not write comments that explain what the code does. Only write a comment when the WHY is non-obvious: a hidden constraint, a workaround for a specific external behaviour, or an invariant that would surprise a reader.
- **Follow PEP 8 and Python idioms.** Use type hints throughout. Use dataclasses for data structures. Use pathlib where paths are involved. Do not use bare `except:`. Do not suppress exceptions silently.
- **Explicit is better than implicit.** No magic. No monkey-patching. No global mutable state.

### Documentation

- Before implementing any module, use **Context7 MCP** to fetch current official docs for the library being used. Do this for every library: httpx, rq, asyncpg, psycopg2, click, pytest-asyncio, respx, beautifulsoup4. Do not rely on training data - library APIs change and training data is stale.
- Fetch docs with: `mcp__context7__resolve-library-id` then `mcp__context7__query-docs` with the specific question (e.g. "httpx proxies configuration", "rq job timeout", "asyncpg connection pool").
- Always conform to the patterns and idioms shown in the official docs, not to patterns from memory.

### Testing

- **TDD strictly.** Write the failing test before writing implementation code. Run it to confirm it fails. Then write the minimum code to make it pass. Never skip this cycle.
- **Do not stop until every test passes and you are confident the code will run in production.** If a test is flaky, fix the underlying issue - do not delete the test or add `pytest.mark.skip`.
- **Test real behaviour, not implementation details.** Tests should assert on outputs and side effects, not on which internal functions were called.
- **Cover failure paths.** Every module that makes a network call or DB write needs tests for failure cases: 404, network timeout, DB unavailable, malformed HTML. Use `respx` for HTTP mocking.
- **Integration test is mandatory.** The full flow (scrape item -> write to DB -> export CSV) must pass before the implementation is considered complete.
- Run the full test suite with `pytest -v` at the end of every task. Do not proceed to the next task if any test is failing.

### Scraper Robustness

- eBay's HTML structure changes. The scraper must not crash on unexpected page structure - return `None` gracefully and let the queue retry.
- JSON-LD parsing must handle missing keys, wrong types, and partial data without raising exceptions.
- All network calls must have explicit timeouts. Never use default (infinite) timeouts.
- Retry logic with exponential backoff on transient failures (5xx, connection errors). Do not retry 404s.

---

## Architecture Summary

- **Coordinator VPS**: runs Redis (rq job queue) + Postgres (results)
- **Worker VPS nodes** (as many as needed): pull item IDs from Redis, scrape eBay, write to Postgres
- **CLI**: `scraper store add <store-url> --niche <tag>` then `scraper scrape start` (crawl + queue) / `scraper init <coordinator-ip>` (one-command worker VPS setup) / `scraper export`. Full command reference in `README.md`.
- Horizontal scaling: add more VPS workers, no code changes needed

## Tech Stack

- Python 3.11+
- `httpx` - HTTP scraping (sync client in workers)
- `beautifulsoup4` - HTML parsing
- `rq` - Redis job queue
- `psycopg2-binary` - sync DB writes in workers (intentional: rq workers must be sync)
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
- `mpn`: Manufacturer Part Number extracted from item_specifics keys "MPN", "Manufacturer Part Number"
- `upc`: UPC/EAN barcode extracted from item_specifics keys "UPC", "EAN"
- `niche`: tag set at scrape time, used to filter exports

## Key Implementation Notes

- Workers use **psycopg2 (sync)** not asyncpg. This is intentional: rq runs sync functions, using asyncpg inside rq workers causes event loop conflicts.
- CLI commands that read/export use **asyncpg (async)** wrapped in `asyncio.run()`.
- Deduplication: a Redis set (`scraped_items`) tracks queued item IDs. `scraper add` skips IDs already in the set.
- Scraper parses **JSON-LD** (`<script type="application/ld+json">`) from eBay item pages - more stable than CSS selectors.
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

# eBay Scraper Scale Redesign

Date: 2026-06-04
Status: Design, pending approval
Supersedes performance assumptions in: docs/superpowers/specs/2026-06-03-ebay-scraper-design.md (architecture and parsing stay; throughput model changes)

## Problem

The scraper works correctly but is far too slow and would be too expensive to run at the
target volume. On a live test, adding `watchadoption` queued ~1,000 items and a worker
scraped only ~13 of them in 5 minutes (about 2.3 items/min per box). The concern was that
hitting millions of products a day would require either huge proxy spend or a fundamentally
different approach.

We need full product data for every item (title, price, condition, description, item
specifics, MPN, UPC, images, etc.), the volume shape is a one-time backfill per store
followed by cheap daily deltas, and the running budget is roughly $100 to $300 per month.

## Diagnosis (validated against live eBay on 2026-06-04)

A low-volume spike against `ebay.com.au/str/watchadoption` from a plain NZ residential IP,
no proxy, using the project's existing Chrome-impersonating `curl_cffi` session:

| Observation | Result | Implication |
|---|---|---|
| Listing page (`_ipg=240`) | 200 to 204 items per request, 2.4 MB, no challenge | Discovery is cheap: ~200 IDs per request |
| Store true size | ~200/page for 5 pages, then a degraded 107-item fallback with no next link | The "1,000" is the real store size, not a bug. Crawler stops correctly |
| Detail page weight | ~760 KB each | Bandwidth, not request count, is the real cost driver |
| Detail page from plain IP | HTTP 200, no challenge, JSON-LD + item specifics present | Item specifics come from the main page; cheap IPs work for detail pages |
| 8 concurrent detail fetches, one IP | 5.5 items/sec, 0 challenges | A single untuned IP does ~475k/day. Current ~0.04/sec is pure self-throttle |

Two distinct problems were being conflated:

1. **Slowness now** is self-inflicted: one rq worker per box, one item per job, a hard
   `0.5 req/sec` limit plus an extra 1-second sleep baked into the fetch. That is the
   ~2.3 items/min, exactly as configured. eBay is not fighting us.
2. **Cost at high volume** is the per-item detail-page architecture: ~760 KB per item is
   real residential-proxy bandwidth if routed through paid residential IPs.

The fix for (1) is concurrency and sane rate limits. The fix for (2) is to run the heavy
detail fetches on the cheap IPs the VPS already has, and reserve paid residential proxy as
an escalation path only when an IP starts getting challenged.

## Goals

- Lift per-box throughput from ~2 items/min to tens of items/sec.
- Keep running cost inside ~$100 to $300/mo by defaulting to the VPS's own bandwidth and
  using residential proxy only as a challenge-triggered fallback.
- Add a delta mode so steady-state re-scraping is cheap (re-fetch only new or changed items).
- Handle stores larger than eBay's browse ceiling (~10k items) via partitioning.
- Preserve the existing parsing (`scraper.py`), challenge detection, country pinning, and
  wrong-country price guard. This is a throughput and orchestration redesign, not a parser
  rewrite.

## Non-goals

- No official eBay Browse API. It cannot filter by seller and omits item specifics,
  description, MPN, and UPC, so it does not fit the data requirement.
- No third-party scraping service.
- No JavaScript rendering or headless browser. The data is in the served HTML.
- No change to the CSV schema or the parsed `ProductData` fields.

## Architecture

Coordinator (Redis + Postgres) and worker VPS nodes stay. The unit of work changes from
one item per rq job to a batch of item IDs per rq job, fetched concurrently inside the job
and bulk-written to Postgres.

```
CLI (coordinator)
  scrape start  -> crawl store listing pages -> collect item IDs
                -> chunk into batches of ~200 -> enqueue batch jobs
  scrape delta  -> re-crawl listing pages -> diff (id, price) vs DB
                -> enqueue only new/changed item IDs as batch jobs
                -> mark missing items inactive

Worker VPS (one rq worker process per box, many concurrent fetches per job)
  batch job (list of ~200 IDs)
    -> ThreadPool(WORKER_CONCURRENCY) fetch detail pages
         per fetch: box IP first; on challenge, back off and escalate to residential
         shared token-bucket caps MAX_RPS_PER_IP
    -> parse each to ProductData (existing parser, unchanged)
    -> bulk upsert to Postgres (execute_values, ON CONFLICT (item_id))
    -> failed items collected and re-enqueued as a smaller retry batch
```

### Components and changes

**`scraper/store.py` (discovery)**
- Keep the existing paginator and challenge handling.
- Add per-listing-card extraction of `(item_id, price)` so delta mode can detect price
  changes without touching detail pages. Listing cards already carry the price.
- Add large-store partitioning: when a crawl reaches eBay's browse ceiling (no next link
  while the store's reported item count is much higher, or item count approaches ~10k),
  re-crawl under price-range partitions (`_udlo`/`_udhi`) and optionally category
  (`_sacat`), then union the IDs. For normal stores (like watchadoption) this never fires.

**`scraper/fetch.py` (IP strategy)**
- Add a fetch helper that tries the box's own IP first and, on a challenge or block,
  retries the same URL through the residential proxy, with the existing backoff.
- Track a rolling per-IP challenge rate. If it crosses a threshold, the box switches all
  fetches to the residential proxy for a cooldown window, then probes direct again.
- Use thread-local `curl_cffi` sessions (one Session per worker thread) for thread safety.
- Reuse `apply_proxy_country` and `expected_currency` unchanged.

**`scraper/worker.py` (batched concurrent execution)**
- Replace the single-item `scrape_and_store` job with a batch job: takes a list of item
  IDs plus niche and store_url, fetches them concurrently with a `ThreadPoolExecutor`
  sized by `WORKER_CONCURRENCY`, applies a shared thread-safe token bucket to cap
  `MAX_RPS_PER_IP`, parses with the existing `scraper.py`, and bulk-upserts the batch.
- Items that fail (challenge after escalation, timeout, parse returns None) are collected
  and re-enqueued as a smaller retry batch so a few bad items never fail the whole batch.
- Keep psycopg2 (sync) per the existing intentional sync/async boundary. No asyncpg here.

**`scraper/queue.py` (batch enqueue + dedup)**
- Enqueue batches of IDs rather than single items.
- Backfill keeps the Redis dedup set (`scraped_items`) so re-running `scrape start` does
  not re-queue already-known IDs.
- Delta bypasses the dedup-set skip and instead diffs against the DB (new or price-changed
  IDs only), then enqueues those as batches.

**`scraper/db.py` (bulk writes + delta state)**
- Bulk upsert via `psycopg2.extras.execute_values` with `ON CONFLICT (item_id) DO UPDATE`.
- Add columns: `last_seen_at` (timestamp), `is_active` (bool), and `price` is already
  stored. Delta uses stored `price` vs listing-card `price` to decide a re-fetch, and sets
  `is_active = false` for items no longer present in the store. These are internal delta
  state only; they are not added to the CSV export, so the export schema is unchanged.

**`scraper/cli.py`**
- `scrape start`: backfill (crawl + batch enqueue), unchanged command name.
- `scrape delta`: new command, listing-page diff + selective enqueue + mark-inactive.
- Worker command unchanged; it now consumes batch jobs.

**`scraper/config.py`**
- New settings with safe defaults tuned to the spike:
  - `WORKER_CONCURRENCY` (threads per box), default conservative (e.g. 8).
  - `MAX_RPS_PER_IP`, default a safe per-IP cap (e.g. 6).
  - `BATCH_SIZE`, default 200.
  - `CHALLENGE_ESCALATION_THRESHOLD` (fraction of recent fetches challenged before
    switching a box to residential) and a cooldown duration.
- `REQUESTS_PER_SECOND` and the 1-second fetch sleep are retired in favour of the
  per-IP token bucket.

## Data flow

**Backfill (`scrape start`)**
1. Crawl store listing pages, partitioning large stores, to collect all item IDs.
2. Filter out IDs already in the dedup set, add the rest, chunk into batches of `BATCH_SIZE`.
3. Enqueue batch jobs. Workers fetch concurrently on box IPs, bulk-upsert, set
   `last_seen_at` and `is_active = true`.

**Delta (`scrape delta`)**
1. Re-crawl listing pages, extracting `(item_id, price)` per card (cheap, ~200/request).
2. Diff against DB: IDs not seen before, or with a changed price, are enqueued for detail
   fetch. IDs in DB but absent from the store are set `is_active = false`.
3. Unchanged items are skipped entirely. No detail-page hit.

## IP strategy and cost model

- Default path: fetch on the VPS's own IP. The spike shows detail pages do not challenge a
  plain IP at modest concurrency, so backfill bandwidth is the VPS's included/cheap traffic.
- Escalation: on a per-fetch challenge, retry via the residential proxy. If a box's recent
  challenge rate crosses the threshold, route the whole box through residential for a
  cooldown, then probe direct again.
- Cost: a ~1,000-item store backfill is ~0.8 GB on the box's own bandwidth. A few-million
  item one-time backfill across cheap VPS bandwidth stays well inside budget; residential
  GBs are spent only on challenged items. Steady-state deltas are tiny.

## Error handling

- Challenge pages (HTTP 200 with the known markers) are detected as today and trigger
  escalation, never treated as "no more data".
- A batch never fails wholesale: per-item failures are isolated, collected, and re-enqueued
  as a smaller retry batch with backoff. rq retry/`with_scheduler` semantics are preserved.
- Wrong-country price guard (existing) still rejects and retries items whose currency does
  not match the site.
- DB writes are bulk and idempotent (upsert on `item_id`), so a re-run or partial retry
  cannot create duplicates.

## Testing (TDD, respx/mocks, no live eBay in the suite)

- Batch worker: fetches a batch concurrently, bulk-upserts all items, and a partial failure
  re-enqueues only the failed IDs.
- Token bucket: caps effective requests/sec per IP across threads.
- IP escalation: a challenged fetch retries through residential; a box over the challenge
  threshold switches to residential then probes direct after cooldown.
- Delta diff: new ID enqueued, price-changed ID enqueued, unchanged ID skipped, missing ID
  marked inactive.
- Listing-card price extraction from fixture HTML.
- Large-store partitioning: ceiling detection triggers price-range re-crawl and IDs union.
- Bulk upsert idempotency: re-running a batch does not duplicate rows.
- Integration: mock store crawl -> batch enqueue -> concurrent fetch -> bulk write ->
  CSV export, plus a delta run that picks up exactly the changed items.

## Migration and compatibility

- `scraper.py` parser, challenge detection, country pinning, and the currency guard are
  unchanged and reused.
- The single-item job path is replaced by the batch job path. The CLI command names for
  the user stay the same (`scrape start`, worker start, export), with `scrape delta` added.
- DB migration adds `last_seen_at` and `is_active` columns; existing rows backfill to
  `is_active = true` on first delta run.

## Open knobs (tunable, not blocking)

- Default `WORKER_CONCURRENCY` and `MAX_RPS_PER_IP` start conservative and can be raised per
  box once real challenge rates are observed in production.
- Whether to keep fetching the description iframe (a second, smaller request) per item, or
  drop it if Kieran's downstream does not use the raw seller description. Kept by default
  because the data requirement is full detail.
```

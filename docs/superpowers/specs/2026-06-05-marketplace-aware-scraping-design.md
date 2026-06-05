# Marketplace-aware scraping + handoff hardening — design

Date: 2026-06-05
Status: approved for planning
Supersedes the "hard ~20%" framing in `docs/findings/2026-06-05-hard-stores-are-marketplace-mismatch.md` (which this design implements the fix for).

## Goal

Kieran's ~1500 eBay stores span the US, AU, and UK marketplaces. Scrape millions of
products across all of them at a **success rate well above 90%**, output CSVs for
Shopify import, and hand the whole thing to a **non-technical operator** who pastes a
few credentials and runs simple commands. Runs unattended for years.

## Operator model (the whole point)

Kieran's entire job is:
1. `scraper setup` once — paste proxy (and optional Oxylabs) credentials.
2. Drop a **plain text file: one eBay store URL per line.** No location, no niche, no
   per-store configuration. He does not need to know or care which marketplace a store
   is on.
3. `scraper run <file>` — one command. The system imports the stores, auto-detects each
   store's home marketplace, scrapes every product across all marketplaces, and writes
   **split CSV files** of all products at the end. Live progress + a final summary.

Everything between dropping the file and getting CSVs is automatic. **Niche tagging is
removed** from the required flow — export is not filtered by niche.

## Root cause being fixed

The stores that returned a fake "0 results" page are sellers whose home eBay marketplace
is not `ebay.com` (e.g. Australian sellers on `ebay.com.au`). eBay serves a seller's
full item grid only on their home domain, to an in-country viewer. The scraper crawled
every seller on `ebay.com` from US IPs, so non-US sellers genuinely returned 0. Proven
live: both known-hard stores (`goodpricepeople`, `af2018`) return their full 240-item
grid through the existing IPRoyal proxy on `ebay.com.au` with an AU exit IP, no challenge.

The country-pinning half already exists (`apply_proxy_country` in `scraper/fetch.py`).
The missing half is **routing each seller to their home marketplace domain**.

## Non-goals (YAGNI)

- No multi-provider unblocker abstraction. Oxylabs only, behind one clean function.
- No marketplace-from-profile detection (proven unreliable: the `.com` page does not
  leak the home domain). Detection is an active probe.
- No FX conversion in export. The `currency` column already carries the true per-item
  currency from JSON-LD.
- No rewrite of the batched worker, throttle, or item parser (already TLD-agnostic).
- Niche is no longer required input. The `niche` column stays in the schema/CSV for
  backward compatibility (defaults to empty), but `store import` and `run` never ask for
  it and export never filters on it.

---

## Components

### 1. Marketplace detection — `scraper/marketplace.py` (new)

```
CANDIDATE_MARKETPLACES = [
    ("www.ebay.com",    "us"),
    ("www.ebay.com.au", "au"),
    ("www.ebay.co.uk",  "gb"),
]   # ordered, extensible; covers Kieran's three marketplaces
```

- `detect_marketplace(seller_id, proxy_url, candidates, fetch_fn=...) -> MarketplaceResult | None`
- For each candidate domain, build the seller-search page-1 URL
  (`https://{domain}/sch/i.html?_ssn={seller_id}&_pgn=1&_ipg=60`) and fetch it.
  `_ipg=60` keeps detection bandwidth down; we only need an item-count signal.
- Probe candidates **concurrently** (thread pool). Count items via the existing
  `scraper.store._extract_item_urls`.
- **Challenge vs genuine-zero split (critical):** a probe that raises `ChallengeError`
  or is detected as a challenge page is **retried** (fresh session = fresh exit IP), up
  to a small bound. A challenged probe is never counted as 0. Only a clean 200 page with
  zero items counts as a genuine zero for that domain.
- Return the domain with the **most** items (`MarketplaceResult(domain, country,
  item_count, seller_search_url)`). This handles cross-listed sellers: the home
  marketplace has the most items.
- Return `None` only when every candidate returns a clean genuine-zero. When some
  candidates are still challenged after retries, surface that as "undetermined"
  (distinct from None) so the caller can fall back to the unblocker rather than mark the
  store empty.

`MarketplaceResult` is a `@dataclass(frozen=True)`.

### 2. Store schema — `scraper/db.py`

Add nullable columns to `stores`:

```
marketplace_domain   TEXT
marketplace_country  TEXT
detected_at          TIMESTAMPTZ
```

New CRUD:
- `set_store_marketplace(pool, store_url, domain, country)` — persists detection.
- `list_stores` returns the marketplace fields.
- Schema creation is idempotent (`ADD COLUMN IF NOT EXISTS`) so existing coordinator DBs
  upgrade in place with no migration step for Kieran.

### 3. Discovery routing — `scraper/cli.py` `_discover_store`

Flow per store:
1. If the store row has a known `marketplace_domain`: crawl that domain's seller-search
   directly (skip detection).
2. Else run `detect_marketplace`:
   - **detected** → persist `(domain, country)`, crawl that domain.
   - **None (genuine empty everywhere)** → if unblocker configured, try one unblocker
     probe across candidates; else mark store empty → `failed_stores.txt`.
   - **undetermined (still challenged)** → if unblocker configured, escalate; else mark
     blocked → `failed_stores.txt` for `scrape retry`.
3. The crawl yields item URLs already on the correct TLD, so detail fetches inherit the
   right domain and `apply_proxy_country` pins their country automatically.

The existing proxy-first / coordinator-IP fallback and per-store OK/empty/blocked
reporting are preserved and extended to print the detected marketplace and item count.

### 4. Optional unblocker fallback — `scraper/unblocker.py` (new)

- `fetch_via_unblocker(url, config) -> str | None`: POSTs to Oxylabs realtime
  (`https://realtime.oxylabs.io/v1/queries`) with `{"source": "ebay", "url": url,
  "render": "html", "geo_location": <country name>}`, returns rendered HTML.
- **OFF by default.** Active only when Oxylabs credentials are configured.
- Config: `unblocker_provider` (`none`|`oxylabs`), `oxylabs_username`, `oxylabs_password`.
  Stored in Redis on the coordinator (like `proxy_url`) so it is set once and shared by
  all workers. Also readable from env for local runs.
- Used in two places, both bounded to *after* proxy attempts are exhausted: (a) detection
  when proxy probes are undetermined/empty, (b) item-fetch escalation for a store that
  persistently challenges through the proxy.
- **No hard budget cap** (operator owns the cost, by explicit decision), but every run
  reports a **visible unblocker-escalation counter** (`N stores / M requests via
  unblocker`) so spend is never invisible.
- Clean single-function boundary so a different provider could replace it later without
  touching callers. Implement Oxylabs only.

### 5. Worker escalation — `scraper/worker.py`

`BoxProxyState` currently escalates own-IP → proxy on a block. Add a bounded third tier:
proxy → unblocker for a batch whose items persistently challenge after proxy retries,
only when the unblocker is configured. Increment the escalation counter. Keep escalation
the exception, not the default path, to control cost.

### 6. Onboarding + one-command run — `scraper/cli.py`

- `scraper setup` — interactive wizard (Click prompts). Collects and validates:
  - coordinator IP (or "this machine"),
  - proxy URL (paste IPRoyal creds) → validated by a real test fetch through it,
  - optional Oxylabs username/password → validated with one test query,
  and writes them to Redis / `.env`. Each step prints a clear green/red result.
- `scraper doctor` — checks Redis, Postgres, proxy (live fetch), unblocker (if set), and
  prints a green/red panel. Lets Kieran self-diagnose without reading logs.
- **`scraper run <file>` — the one command Kieran uses.** A blocking orchestrator that
  chains the whole pipeline and shows live progress:
  1. **Import**: read the file (one store URL per line; blank lines and `#` comments
     skipped), dedupe, upsert into `stores`. No niche prompt.
  2. **Discover + queue**: for each store, resolve its marketplace (detect or use the
     persisted one), crawl, and enqueue `scrape_batch` jobs. Per-store line:
     `OK | marketplace | item-count` (or empty/blocked).
  3. **Wait for drain**: poll the rq queue until all batches finish (with a live
     count of remaining/done and a periodic heartbeat), so the command returns only when
     scraping is actually complete.
  4. **Export**: write split CSVs automatically (see Output).
  5. **Final summary**: `X/Y stores OK, Z products, N via unblocker, W CSV files written`.
  `run` requires workers to be running (on VPSs, or locally for a single-box setup);
  `doctor` confirms at least one worker is connected before a long run starts.
  Flags: `--export-dir`, `--rows-per-file`, `--no-wait` (queue and exit without blocking).
- The existing granular commands (`store import`, `scrape start`, `scrape delta`,
  `export`) remain for power use; `run` is the simple front door that composes them.
- `scrape start` / `scrape retry` output also gains the per-store `OK | marketplace |
  item-count` line and the final **success-rate summary**.

### 7. Validation harness + sample list

- `tests/fixtures/sample_stores.csv` — a composed list of **real** US/AU/UK stores
  (including the known-hard AU ones) for the live validation run. Kieran swaps in his
  real list later via the same `store import` command.
- A documented end-to-end validation run that **stresses volume and concurrency** (many
  stores, real batched detail scraping at the configured RPS), not just routing
  correctness on a handful of stores. The >90%-on-millions bar rests on rotating-
  residential IP reputation under sustained load — the run must exercise that axis and
  the result is recorded.

---

## Output / currency

- **Auto-split CSVs.** Export writes all products across numbered files
  (`products_001.csv`, `products_002.csv`, ...) capped at `--rows-per-file` rows each
  (default 500,000, configurable), because spreadsheet/import tools choke on multi-
  million-row single files. Each file carries the full header row. No niche filter —
  every scraped product is exported.
- **Unchanged column structure**, including the existing `currency` column which carries
  the accurate per-item currency from JSON-LD (USD / AUD / GBP). **README and HANDOFF
  must state loudly** that the output is multi-currency and Kieran must segment or convert
  by the `currency` column before mapping `price` into a single-currency Shopify store,
  to avoid importing AUD/GBP numbers as USD.

## Testing (TDD)

- `detect_marketplace`: picks max-item domain; challenge≠0 (challenged probe retried,
  not counted as zero); all-genuine-zero → None; some-still-challenged → undetermined.
- `unblocker`: respx-mocked Oxylabs realtime endpoint — success returns HTML, auth/error
  paths return None, counter increments.
- DB: new marketplace columns persist and round-trip; idempotent schema upgrade.
- Routing: known domain skips detection; unknown triggers detect+persist; empty →
  failed; undetermined+unblocker → escalates.
- Worker: proxy → unblocker escalation only after proxy retries, only when configured.
- Export split: N products across `--rows-per-file` boundary → correct file count, every
  file has the header, row totals match, no niche filtering.
- `run` orchestrator: file parsing (comments/blank lines skipped, deduped), pipeline
  composition (import → discover → wait → export) with the queue-drain wait mockable;
  `--no-wait` queues and exits.
- All existing tests stay green (the niche-required behaviour changes to niche-optional;
  update those tests accordingly rather than deleting them). Full suite via the
  documented Docker + pytest flow.

## Risks / honesty

- The unblocker residual is currently **hypothetical** (n=2 known-hard stores, both fixed
  for free). The fallback is built but OFF-by-default and separable; the validation run
  decides whether the residual exists before any escalation tuning is trusted.
- Detection cost: ~3 probes × `_ipg=60` per new store, one-time, persisted. Negligible
  at 1500 stores.
- The single-IP reputation ceiling (~3,500 fast requests) is unchanged by this work;
  millions/day still means more residential boxes at a modest `MAX_RPS_PER_IP`.

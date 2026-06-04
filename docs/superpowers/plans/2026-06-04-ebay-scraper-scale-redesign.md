# eBay Scraper Scale Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Lift the eBay scraper from ~2 items/min/box to several items/sec/box (~3/s at safe defaults, ~130x the old rate, since each item is 2 requests and the per-IP cap defaults to 6 req/s), with throughput scaling linearly by adding boxes. Keep running cost inside ~$100-300/mo by fetching on the box's own IP and escalating to residential proxy only on challenge, and add a cheap delta mode so steady-state re-scraping only re-fetches new or changed items. Ship it tested on real infra and ready to hand to Kieran.

**Architecture:** The unit of work changes from one item per rq job to a batch of ~200 item URLs per rq job, fetched concurrently inside the job with a thread pool, rate-limited by a shared per-IP token bucket, and bulk-upserted to Postgres. Each box runs exactly one rq worker process (its single IP is governed by the token bucket); scale = more boxes. Discovery and delta read eBay store listing pages (~200 items/request). Residential proxy is a challenge-triggered fallback, not the default path.

**Tech Stack:** Python 3.11+, curl_cffi (Chrome TLS impersonation), beautifulsoup4, rq + Redis, psycopg2 (sync writes in workers), asyncpg (async reads in CLI), click, pytest + respx. No new dependencies (ThreadPoolExecutor and psycopg2.extras.execute_values are stdlib/existing).

---

## Validated facts this plan is built on (live eBay + infra, 2026-06-04)

- A plain IP (no proxy) fetched 8 detail pages concurrently at **5.5 items/sec, 0 challenges**. Detail pages are **~760 KB** each. Item specifics + JSON-LD are on the main page; only the seller description needs the iframe (2nd request).
- Store listing pages return ~200 cards/request. Cards use container class **`.str-item-card`**; **218/221** carry both an `/itm/<id>` link and a price. This is the basis for delta + discovery.
- `watchadoption` genuinely has ~1,000 items (5 pages, then a degraded fallback). Not a bug.

## Infra map (for the E2E phase)

- **Coordinator:** Fabien's Mac. Redis + Postgres already running via Docker (`ebay-scraper-redis-1` on 6379, `ebay-scraper-postgres-1` on 5432). DB `ebayscraper`, user `scraper`. Local `.env` at `~/.config/ebay-scraper/.env`.
- **Worker:** VPS `automation-management`, Tailscale IP `100.70.136.110`, Linux x86_64, Python 3.11, `scraper` installed at `/usr/local/bin/scraper`, code at `/root/ebay-scraper` (not a git checkout). Currently runs 2 loose `scraper worker start` processes (Phase H reduces this to one managed worker).
- **Proxy:** IPRoyal residential. Set at runtime via `scraper proxy set <url>` (stored in Redis key `ebay-scraper:proxy_url`, read by workers per job). The URL is a Phase-I prerequisite to obtain from Fabien.
- **Connectivity:** Worker reaches coordinator over Tailscale (`REDIS_URL`/`DATABASE_URL` point at the Mac's Tailscale IP).

## Prerequisite (do before Phase A)

- [ ] **Create an isolated worktree** via superpowers:using-git-worktrees, branch `scale-redesign`. Confirm baseline tests pass first:

Run: `cd /Users/fabienbrocklesby/code/general-projects/ebay-scraper && docker compose -f coordinator-compose.yml up -d 2>/dev/null; docker exec ebay-scraper-postgres-1 psql -U scraper -c "CREATE DATABASE ebayscraper_test;" 2>/dev/null; pytest -q`
Expected: existing suite passes (47 tests green). Record the count.

## File structure

| File | Responsibility | Change |
|---|---|---|
| `scraper/throttle.py` | `TokenBucket` (per-IP RPS cap) + `BoxProxyState` (challenge-rate -> residential switch). No deps on scraper.py/fetch.py. | Create |
| `scraper/config.py` | Settings. Add concurrency/rate/batch/escalation knobs. | Modify |
| `scraper/worker.py` | Batched concurrent job `scrape_batch` (replaces `scrape_and_store`), per-item escalation, sync bulk upsert, failed-item requeue. | Major modify |
| `scraper/db.py` | Add `last_seen_at`/`is_active` columns; async delta reads (`get_store_item_prices`, `mark_items_inactive`). | Modify |
| `scraper/queue.py` | Chunk item URLs into batches and enqueue `scrape_batch`. | Modify |
| `scraper/store.py` | `get_store_listings` (id+price+url per card) for delta; large-store price-range partitioning in discovery. | Modify |
| `scraper/delta.py` | Pure `compute_delta(listings, db_prices) -> (to_fetch, to_deactivate)`. | Create |
| `scraper/cli.py` | `scrape delta` command; `scrape start` uses batched enqueue. | Modify |
| `deploy/ebay-worker.service` | systemd unit for one managed worker per box. | Create |
| `tests/test_throttle.py`, `tests/test_delta.py`, `tests/test_store_listings.py` | New unit tests. | Create |
| `tests/test_worker.py`, `tests/test_queue.py`, `tests/test_db.py`, `tests/test_integration.py` | Update for batch model + new columns. | Modify |
| `tests/fixtures/store_listing.html` | Captured real listing page for the parser test. | Create |
| `README.md` | Document delta, concurrency knobs, one-worker-per-box, deploy. | Modify |

---

## Phase A: Foundations (config + throttle primitives)

### Task A1: TokenBucket (thread-safe per-IP rate limiter)

**Files:**
- Create: `scraper/throttle.py`
- Test: `tests/test_throttle.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_throttle.py
import time
from scraper.throttle import TokenBucket


def test_token_bucket_caps_rate():
    bucket = TokenBucket(rate_per_sec=10.0)
    start = time.monotonic()
    for _ in range(20):
        bucket.acquire()
    elapsed = time.monotonic() - start
    # 20 tokens at 10/sec, first ~capacity are immediate, rest paced. Expect >= ~0.9s.
    assert elapsed >= 0.9


def test_token_bucket_thread_safe():
    import concurrent.futures as cf
    bucket = TokenBucket(rate_per_sec=50.0)
    start = time.monotonic()
    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(lambda _: bucket.acquire(), range(100)))
    elapsed = time.monotonic() - start
    assert elapsed >= 1.0  # 100 tokens / 50 per sec, minus initial burst
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_throttle.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'scraper.throttle'`

- [ ] **Step 3: Write minimal implementation**

```python
# scraper/throttle.py
"""Thread-safe primitives that govern one box's single outbound IP.

A box runs one rq worker process that fetches a batch of items concurrently.
`TokenBucket` caps the box's request rate so a single IP stays under eBay's
per-IP reputation threshold. `BoxProxyState` watches the recent challenge rate
and flips the whole box onto the residential proxy when the box IP starts
getting blocked, then probes the direct path again after a cooldown.
"""

import threading
import time
from collections import deque


class TokenBucket:
    def __init__(self, rate_per_sec: float, capacity: float | None = None):
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be positive")
        self._rate = rate_per_sec
        self._capacity = capacity if capacity is not None else max(1.0, rate_per_sec)
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._updated) * self._rate
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                deficit = 1.0 - self._tokens
                wait = deficit / self._rate
            time.sleep(wait)


class BoxProxyState:
    """Tracks a rolling window of recent fetches and decides direct vs residential."""

    def __init__(self, threshold: float, cooldown_seconds: float, window: int = 50):
        self._threshold = threshold
        self._cooldown = cooldown_seconds
        self._window = window
        self._recent: deque[bool] = deque(maxlen=window)
        self._cooldown_until = 0.0
        self._lock = threading.Lock()

    def record(self, challenged: bool) -> None:
        with self._lock:
            self._recent.append(challenged)
            if challenged and self._challenge_rate() >= self._threshold:
                self._cooldown_until = time.monotonic() + self._cooldown

    def should_use_residential(self) -> bool:
        with self._lock:
            return time.monotonic() < self._cooldown_until

    def _challenge_rate(self) -> float:
        if not self._recent:
            return 0.0
        return sum(1 for c in self._recent if c) / len(self._recent)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_throttle.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add scraper/throttle.py tests/test_throttle.py
git commit -m "feat: add TokenBucket and BoxProxyState throttle primitives"
```

### Task A2: BoxProxyState escalation behaviour test

**Files:**
- Test: `tests/test_throttle.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_throttle.py
from scraper.throttle import BoxProxyState


def test_box_state_escalates_after_challenge_threshold():
    state = BoxProxyState(threshold=0.2, cooldown_seconds=5.0, window=10)
    assert state.should_use_residential() is False
    for _ in range(3):  # 3/n crosses 0.2 once n small; records challenged
        state.record(challenged=True)
    assert state.should_use_residential() is True


def test_box_state_recovers_after_cooldown():
    state = BoxProxyState(threshold=0.5, cooldown_seconds=0.2, window=4)
    state.record(challenged=True)
    state.record(challenged=True)
    assert state.should_use_residential() is True
    time.sleep(0.25)
    assert state.should_use_residential() is False
```

- [ ] **Step 2: Run test to verify it fails or passes**

Run: `pytest tests/test_throttle.py -v`
Expected: PASS (the A1 implementation already satisfies these; if any fail, fix `BoxProxyState` thresholds logic until green). This task locks the escalation contract.

- [ ] **Step 3: Commit**

```bash
git add tests/test_throttle.py
git commit -m "test: lock BoxProxyState escalation and cooldown contract"
```

### Task A3: Config knobs

**Files:**
- Modify: `scraper/config.py:23-40`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py (add)
def test_settings_concurrency_defaults(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.delenv("WORKER_CONCURRENCY", raising=False)
    monkeypatch.delenv("MAX_RPS_PER_IP", raising=False)
    monkeypatch.delenv("BATCH_SIZE", raising=False)
    from scraper.config import Settings
    s = Settings()
    assert s.worker_concurrency == 8
    assert s.max_rps_per_ip == 6.0
    assert s.batch_size == 200
    assert s.challenge_escalation_threshold == 0.15
    assert s.challenge_cooldown_seconds == 120.0


def test_settings_concurrency_overrides(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setenv("WORKER_CONCURRENCY", "16")
    monkeypatch.setenv("MAX_RPS_PER_IP", "10")
    monkeypatch.setenv("BATCH_SIZE", "100")
    from scraper.config import Settings
    s = Settings()
    assert s.worker_concurrency == 16
    assert s.max_rps_per_ip == 10.0
    assert s.batch_size == 100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py -v`
Expected: FAIL with `AttributeError: 'Settings' object has no attribute 'worker_concurrency'`

- [ ] **Step 3: Add fields to Settings**

In `scraper/config.py`, extend the dataclass annotations and `__init__`:

```python
@dataclass
class Settings:
    redis_url: str
    database_url: str
    proxy_url: str | None
    requests_per_second: float
    worker_concurrency: int
    max_rps_per_ip: float
    batch_size: int
    challenge_escalation_threshold: float
    challenge_cooldown_seconds: float

    def __init__(self):
        # ... keep existing redis_url / database_url / proxy_url / requests_per_second ...
        self.worker_concurrency = int(os.getenv("WORKER_CONCURRENCY", "8"))
        self.max_rps_per_ip = float(os.getenv("MAX_RPS_PER_IP", "6"))
        self.batch_size = int(os.getenv("BATCH_SIZE", "200"))
        self.challenge_escalation_threshold = float(
            os.getenv("CHALLENGE_ESCALATION_THRESHOLD", "0.15")
        )
        self.challenge_cooldown_seconds = float(
            os.getenv("CHALLENGE_COOLDOWN_SECONDS", "120")
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scraper/config.py tests/test_config.py
git commit -m "feat: add concurrency, per-IP rate, batch, and escalation settings"
```

---

## Phase B: DB schema + sync bulk upsert

### Task B1: Add delta-state columns to schema

**Files:**
- Modify: `scraper/db.py:28-48`
- Test: `tests/test_db.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db.py (add)
import pytest


@pytest.mark.asyncio
async def test_products_table_has_delta_columns(db_pool):
    rows = await db_pool.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name='products'"
    )
    cols = {r["column_name"] for r in rows}
    assert "last_seen_at" in cols
    assert "is_active" in cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_db.py::test_products_table_has_delta_columns -v`
Expected: FAIL (columns absent)

- [ ] **Step 3: Add columns to the CREATE TABLE**

In `scraper/db.py`, append to the `products` table definition:

```sql
    last_seen_at TIMESTAMPTZ DEFAULT now(),
    is_active    BOOLEAN DEFAULT true
```

And make `init_schema` idempotently add them for existing tables (after the CREATE statements):

```python
await pool.execute(
    "ALTER TABLE products ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ DEFAULT now()"
)
await pool.execute(
    "ALTER TABLE products ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT true"
)
```

- [ ] **Step 4: Recreate the test DB schema and run**

Run: `docker exec ebay-scraper-postgres-1 psql -U scraper -c "DROP DATABASE IF EXISTS ebayscraper_test;" -c "CREATE DATABASE ebayscraper_test;" ; pytest tests/test_db.py::test_products_table_has_delta_columns -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scraper/db.py tests/test_db.py
git commit -m "feat: add last_seen_at and is_active columns for delta tracking"
```

### Task B2: Sync bulk upsert in worker

**Files:**
- Modify: `scraper/worker.py`
- Test: `tests/test_worker.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_worker.py (add)
from unittest.mock import MagicMock
from scraper.scraper import ProductData


def _pd(item_id):
    return ProductData(
        item_id=item_id, title="t", price=1.0, currency="AUD", condition="New",
        description="d", image_urls="u", item_url=f"https://www.ebay.com.au/itm/{item_id}",
        seller_id="s", category="c", item_specifics="{}", mpn="", upc="",
        shipping="", listing_type="FixedPrice",
    )


def test_bulk_upsert_executes_values_once(monkeypatch):
    import scraper.worker as w
    captured = {}

    def fake_execute_values(cur, sql, rows, template=None, page_size=100):
        captured["sql"] = sql
        captured["rows"] = list(rows)

    mock_conn = MagicMock()
    monkeypatch.setattr(w.psycopg2, "connect", lambda dsn: mock_conn)
    monkeypatch.setattr(w, "execute_values", fake_execute_values)

    w._bulk_upsert("postgresql://x/y", [_pd("111111111111"), _pd("222222222222")], "watch", "https://store")

    assert len(captured["rows"]) == 2
    assert "ON CONFLICT (item_id)" in captured["sql"]
    assert "is_active" in captured["sql"]
    assert mock_conn.commit.called
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_worker.py::test_bulk_upsert_executes_values_once -v`
Expected: FAIL (`_bulk_upsert` / `execute_values` not defined in worker)

- [ ] **Step 3: Implement `_bulk_upsert`**

Add to `scraper/worker.py` (top-level imports and function):

```python
import psycopg2
from psycopg2.extras import execute_values

_BULK_COLUMNS = (
    "item_id, title, price, currency, condition, description, image_urls, item_url, "
    "seller_id, store_url, category, item_specifics, mpn, upc, shipping, listing_type, "
    "niche, last_seen_at, is_active"
)

_BULK_UPSERT_SQL = f"""
    INSERT INTO products ({_BULK_COLUMNS})
    VALUES %s
    ON CONFLICT (item_id) DO UPDATE SET
        title = EXCLUDED.title,
        price = EXCLUDED.price,
        currency = EXCLUDED.currency,
        condition = EXCLUDED.condition,
        description = EXCLUDED.description,
        image_urls = EXCLUDED.image_urls,
        item_url = EXCLUDED.item_url,
        seller_id = EXCLUDED.seller_id,
        store_url = EXCLUDED.store_url,
        category = EXCLUDED.category,
        item_specifics = EXCLUDED.item_specifics,
        mpn = EXCLUDED.mpn,
        upc = EXCLUDED.upc,
        shipping = EXCLUDED.shipping,
        listing_type = EXCLUDED.listing_type,
        niche = EXCLUDED.niche,
        last_seen_at = now(),
        is_active = true
"""

_BULK_TEMPLATE = (
    "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now(), true)"
)


def _bulk_upsert(database_url: str, products: list[ProductData], niche: str, store_url: str) -> None:
    rows = [
        (
            p.item_id, p.title, p.price, p.currency, p.condition, p.description,
            p.image_urls, p.item_url, p.seller_id, store_url, p.category,
            p.item_specifics, p.mpn, p.upc, p.shipping, p.listing_type, niche,
        )
        for p in products
    ]
    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            execute_values(cur, _BULK_UPSERT_SQL, rows, template=_BULK_TEMPLATE, page_size=200)
        conn.commit()
    finally:
        conn.close()
```

Add `from scraper.scraper import ProductData` if not already imported.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_worker.py::test_bulk_upsert_executes_values_once -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scraper/worker.py tests/test_worker.py
git commit -m "feat: sync bulk upsert for batched worker writes"
```

---

## Phase C: Batched concurrent worker

### Task C1: Per-item fetch with escalation (inject session to kill warmup + sleep)

**Files:**
- Modify: `scraper/worker.py`
- Test: `tests/test_worker.py`

**Why this shape:** `scrape_item(url)` with no `client` runs a homepage warmup GET **and `time.sleep(1.0)`** per item (`scraper/scraper.py:190-199`), plus the wrong-country guard at line 236 keys off `proxy_url`. We bypass the warmup+sleep by always passing a `client` (a `curl_cffi` session we build per call with `apply_proxy_country`), so the `TokenBucket` is the only rate governor. This is what retires the per-item sleep the spec calls out, while leaving `scrape_item`'s own-client path intact for `proxy test`/standalone use. The session must be a real `curl_cffi` session (Chrome TLS impersonation), not httpx, so it matches the production fetch path.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_worker.py (add)
from scraper.fetch import ChallengeError
from scraper.throttle import TokenBucket, BoxProxyState


def test_scrape_one_injects_session_and_escalates_on_challenge(monkeypatch):
    import scraper.worker as w
    calls = []  # (proxy_url, client_is_not_none)

    monkeypatch.setattr(w, "build_session", lambda proxy=None: f"session::{proxy}")

    def fake_scrape_item(item_url, proxy_url=None, client=None):
        calls.append((proxy_url, client))
        if proxy_url is None:
            raise ChallengeError("blocked")  # direct attempt is challenged
        return _pd("111111111111")

    monkeypatch.setattr(w, "scrape_item", fake_scrape_item)
    bucket = TokenBucket(1000.0)
    state = BoxProxyState(threshold=0.15, cooldown_seconds=10.0)

    result = w._scrape_one(
        "https://www.ebay.com.au/itm/111111111111",
        residential_proxy="http://user:pass@proxy:8080",
        box_state=state, bucket=bucket,
    )
    assert result is not None
    # tried direct first, then residential; a non-None client was injected both times
    assert [c[0] for c in calls] == [None, "http://user:pass@proxy:8080"]
    assert all(c[1] is not None for c in calls)


def test_scrape_one_returns_none_on_404(monkeypatch):
    import scraper.worker as w
    monkeypatch.setattr(w, "build_session", lambda proxy=None: "session")
    monkeypatch.setattr(w, "scrape_item", lambda *a, **k: None)
    bucket = TokenBucket(1000.0)
    state = BoxProxyState(0.15, 10.0)
    assert w._scrape_one("https://www.ebay.com.au/itm/1", None, state, bucket) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_worker.py::test_scrape_one_injects_session_and_escalates_on_challenge -v`
Expected: FAIL (`_scrape_one` not defined)

- [ ] **Step 3: Implement `_scrape_one`**

Add to `scraper/worker.py`:

```python
from scraper.scraper import scrape_item
from scraper.fetch import ChallengeError, WrongCountryError, build_session, apply_proxy_country
from scraper.throttle import TokenBucket, BoxProxyState


def _scrape_one(item_url, residential_proxy, box_state: BoxProxyState, bucket: TokenBucket):
    """Fetch one item. Box IP first; on challenge or wrong country, retry via residential.

    A curl_cffi session is built and injected per attempt, which makes scrape_item
    skip its homepage warmup and 1s sleep (those only run on its own-client path).
    The TokenBucket is therefore the only rate governor. Raises if the residential
    retry also fails so the caller can mark the item failed.
    """
    bucket.acquire()
    use_residential = bool(box_state.should_use_residential() and residential_proxy)
    proxy = residential_proxy if use_residential else None
    try:
        session = build_session(apply_proxy_country(proxy, item_url))
        data = scrape_item(item_url, proxy_url=proxy, client=session)
        box_state.record(challenged=False)
        return data
    except (ChallengeError, WrongCountryError):
        box_state.record(challenged=True)
        if residential_proxy and not use_residential:
            bucket.acquire()
            session = build_session(apply_proxy_country(residential_proxy, item_url))
            return scrape_item(item_url, proxy_url=residential_proxy, client=session)
        raise
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_worker.py::test_scrape_one_injects_session_and_escalates_on_challenge tests/test_worker.py::test_scrape_one_returns_none_on_404 -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scraper/worker.py tests/test_worker.py
git commit -m "feat: per-item fetch injects session (no warmup/sleep) with residential escalation"
```

### Task C2: `scrape_batch` job (concurrent fetch + bulk write + requeue)

**Files:**
- Modify: `scraper/worker.py` (replace `scrape_and_store`)
- Test: `tests/test_worker.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_worker.py (add)
def test_scrape_batch_fetches_concurrently_and_bulk_writes(monkeypatch):
    import scraper.worker as w
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setattr(w, "_get_proxy_url", lambda s: None)

    def fake_scrape_one(url, residential, state, bucket):
        if url.endswith("999"):
            raise Exception("boom")  # one failure
        return _pd(url.rsplit("/", 1)[1])

    written = {}
    requeued = {}
    monkeypatch.setattr(w, "_scrape_one", fake_scrape_one)
    monkeypatch.setattr(w, "_bulk_upsert", lambda db, prods, niche, store: written.update({"n": len(prods)}))
    monkeypatch.setattr(w, "_requeue_failed", lambda urls, niche, store, attempt: requeued.update({"urls": list(urls), "attempt": attempt}))

    urls = [f"https://www.ebay.com.au/itm/{i}" for i in ("111", "222", "999")]
    w.scrape_batch(urls, "watch", "https://store", attempt=0)

    assert written["n"] == 2          # two succeeded, bulk-written together
    assert requeued["urls"] == ["https://www.ebay.com.au/itm/999"]
    assert requeued["attempt"] == 1


def test_scrape_batch_drops_failures_after_max_attempts(monkeypatch):
    import scraper.worker as w
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setattr(w, "_get_proxy_url", lambda s: None)
    monkeypatch.setattr(w, "_scrape_one", lambda *a, **k: (_ for _ in ()).throw(Exception("x")))
    monkeypatch.setattr(w, "_bulk_upsert", lambda *a, **k: None)
    called = {"requeued": False}
    monkeypatch.setattr(w, "_requeue_failed", lambda *a, **k: called.update(requeued=True))
    w.scrape_batch(["https://www.ebay.com.au/itm/1"], "watch", "https://store", attempt=3)
    assert called["requeued"] is False  # at max attempts, give up, no requeue
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_worker.py::test_scrape_batch_fetches_concurrently_and_bulk_writes -v`
Expected: FAIL (`scrape_batch` not defined)

- [ ] **Step 3: Implement `scrape_batch` and `_requeue_failed`; remove `scrape_and_store`**

Replace `scrape_and_store` in `scraper/worker.py` with:

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

_MAX_BATCH_ATTEMPTS = 3


def scrape_batch(item_urls: list[str], niche: str, store_url: str, attempt: int = 0) -> None:
    """rq job: fetch a batch of item URLs concurrently and bulk-upsert the results.

    Failed items are collected and re-enqueued as a smaller batch, up to
    `_MAX_BATCH_ATTEMPTS` total attempts, so a few bad items never fail the whole
    batch and never loop forever.
    """
    settings = Settings()
    residential = _get_proxy_url(settings)
    bucket = TokenBucket(settings.max_rps_per_ip)
    box_state = BoxProxyState(
        settings.challenge_escalation_threshold, settings.challenge_cooldown_seconds
    )

    results: list[ProductData] = []
    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=settings.worker_concurrency) as ex:
        futures = {
            ex.submit(_scrape_one, url, residential, box_state, bucket): url
            for url in item_urls
        }
        for fut in as_completed(futures):
            url = futures[fut]
            try:
                data = fut.result()
                if data is not None:
                    results.append(data)
            except Exception:
                failed.append(url)

    if results:
        _bulk_upsert(settings.database_url, results, niche, store_url)
    if failed and attempt + 1 < _MAX_BATCH_ATTEMPTS:
        _requeue_failed(failed, niche, store_url, attempt + 1)


def _requeue_failed(item_urls: list[str], niche: str, store_url: str, attempt: int) -> None:
    from scraper.queue import get_redis, get_queue
    settings = Settings()
    conn = get_redis(settings.redis_url)
    queue = get_queue(conn)
    queue.enqueue(scrape_batch, item_urls, niche, store_url, attempt, job_timeout=600)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_worker.py -v`
Expected: PASS (update or delete any obsolete `scrape_and_store` tests in the same file as part of this step)

- [ ] **Step 5: Commit**

```bash
git add scraper/worker.py tests/test_worker.py
git commit -m "feat: batched concurrent scrape_batch job with bulk write and bounded requeue"
```

---

## Phase D: Queue batching + wire discovery

### Task D1: Chunk item URLs into batch jobs

**Files:**
- Modify: `scraper/queue.py` (`enqueue_items`)
- Test: `tests/test_queue.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_queue.py (add)
from unittest.mock import MagicMock
from scraper.queue import enqueue_items


def test_enqueue_items_batches_by_size(monkeypatch):
    import scraper.queue as q
    monkeypatch.setattr(q, "_BATCH_SIZE", 2)
    queue = MagicMock()
    redis_conn = MagicMock()
    redis_conn.sismember.return_value = False  # nothing pre-queued
    urls = [f"https://www.ebay.com.au/itm/{i}" for i in ("111", "222", "333")]
    count = enqueue_items(queue, redis_conn, urls, "watch", "https://store")
    assert count == 3                       # 3 items enqueued
    assert queue.enqueue.call_count == 2    # in 2 batches (2 + 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_queue.py::test_enqueue_items_batches_by_size -v`
Expected: FAIL (still enqueues per item / `_BATCH_SIZE` missing)

- [ ] **Step 3: Rewrite `enqueue_items` to batch**

In `scraper/queue.py`:

```python
from scraper.config import Settings

_BATCH_SIZE = Settings().batch_size if False else 200  # default; overridden per-call below


def enqueue_items(queue, redis_conn, item_urls, niche, store_url) -> int:
    import re
    from scraper.worker import scrape_batch

    batch_size = _BATCH_SIZE
    new_urls = []
    for url in item_urls:
        m = re.search(r"/itm/(\d{11,13})", url)
        item_id = m.group(1) if m else url
        if not is_item_queued(redis_conn, item_id):
            mark_item_queued(redis_conn, item_id)
            new_urls.append(url)

    enqueued = 0
    for i in range(0, len(new_urls), batch_size):
        batch = new_urls[i : i + batch_size]
        queue.enqueue(scrape_batch, batch, niche, store_url, 0, job_timeout=600, retry=_JOB_RETRY)
        enqueued += len(batch)
    return enqueued
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_queue.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scraper/queue.py tests/test_queue.py
git commit -m "feat: enqueue items as batched scrape_batch jobs"
```

---

## Phase E: Delta mode

### Task E1: Store listing parser (id + price + url per card)

**Files:**
- Modify: `scraper/store.py`
- Create: `tests/fixtures/store_listing.html`, `tests/test_store_listings.py`

- [ ] **Step 1: Capture a real listing fixture**

Run:
```bash
python3 - <<'PY'
import sys; sys.path.insert(0, ".")
from scraper.fetch import build_session
html = build_session().get("https://www.ebay.com.au/str/watchadoption?_pgn=1&_ipg=240", timeout=30).text
open("tests/fixtures/store_listing.html", "w").write(html)
print("saved", len(html), "bytes")
PY
```
Expected: saves ~2.4 MB. (Captured once; the test reads the file, never hits the network.)

- [ ] **Step 2: Write the failing test**

```python
# tests/test_store_listings.py
from pathlib import Path
from scraper.store import parse_listing_cards

FIXTURE = Path(__file__).parent / "fixtures" / "store_listing.html"


def test_parse_listing_cards_extracts_id_and_price():
    html = FIXTURE.read_text()
    cards = parse_listing_cards(html)
    assert len(cards) >= 180  # ~218 real item cards expected
    item_id, price, url = cards[0]
    assert item_id.isdigit() and 11 <= len(item_id) <= 13
    assert price is None or isinstance(price, float)
    assert "/itm/" in url
    # all ids unique
    assert len({c[0] for c in cards}) == len(cards)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_store_listings.py -v`
Expected: FAIL (`parse_listing_cards` not defined)

- [ ] **Step 4: Implement `parse_listing_cards`**

Add to `scraper/store.py`:

```python
import re
from bs4 import BeautifulSoup

_ITM_RE = re.compile(r"/itm/(\d{11,13})")
_PRICE_RE = re.compile(r"[\d][\d,]*\.\d{2}")


def parse_listing_cards(html: str) -> list[tuple[str, float | None, str]]:
    """Extract (item_id, price, item_url) from each store listing card.

    eBay store cards use the container class `.str-item-card`. A few promo/header
    cards carry no item link and are skipped. Price is parsed from the card text
    (first currency-looking number); None if the card shows no price.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[str, float | None, str]] = []
    seen: set[str] = set()
    for card in soup.select(".str-item-card"):
        link = card.find("a", href=_ITM_RE)
        if not link:
            continue
        m = _ITM_RE.search(link["href"])
        if not m:
            continue
        item_id = m.group(1)
        if item_id in seen:
            continue
        seen.add(item_id)
        pm = _PRICE_RE.search(card.get_text(" ", strip=True))
        price = float(pm.group(0).replace(",", "")) if pm else None
        url = f"https://www.ebay.com.au/itm/{item_id}"
        out.append((item_id, price, url))
    return out
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_store_listings.py -v`
Expected: PASS. If `len(cards)` or price assertions fail, inspect the fixture (`grep -o 'str-item-card' | wc -l`) and adjust the selector/price regex against the real markup until green. Do not weaken the test below 180 cards without confirming the fixture genuinely has fewer.

- [ ] **Step 6: Commit**

```bash
git add scraper/store.py tests/test_store_listings.py tests/fixtures/store_listing.html
git commit -m "feat: parse_listing_cards extracts item id, price, url from store cards"
```

### Task E2: `get_store_listings` (paginated id+price crawl)

**Files:**
- Modify: `scraper/store.py`
- Test: `tests/test_store_listings.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_store_listings.py (add)
import respx, httpx
from scraper.store import get_store_listings

ONE_CARD = (
    '<div class="str-item-card"><a href="/itm/123456789012">x</a> 49.99</div>'
)
EMPTY = '<div>no cards</div>'


@respx.mock
def test_get_store_listings_paginates_until_no_cards():
    base = "https://www.ebay.com.au/str/x"
    respx.get(url__startswith=base).mock(side_effect=[
        httpx.Response(200, text=ONE_CARD),  # page 1: one card
        httpx.Response(200, text=EMPTY),     # page 2: stop
    ])
    listings = get_store_listings(base, _session=httpx.Client())
    assert listings == [("123456789012", 49.99, "https://www.ebay.com.au/itm/123456789012")]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_store_listings.py::test_get_store_listings_paginates_until_no_cards -v`
Expected: FAIL (`get_store_listings` not defined)

- [ ] **Step 3: Implement `get_store_listings`**

Add to `scraper/store.py` (reuse `_page_url`, `is_challenge_page`, challenge recovery patterns already present; mirror the structure of `get_item_urls_from_store`):

```python
def get_store_listings(
    store_url: str,
    proxy_url: str | None = None,
    max_pages: int = 9999,
    max_challenge_retries: int = 4,
    _session: Any | None = None,
) -> list[tuple[str, float | None, str]]:
    """Crawl store listing pages, returning (item_id, price, item_url) per card.

    Cheaper than get_item_urls_from_store for delta: one request yields ~200
    items with their current price, no detail-page hits.
    """
    base = _normalize_store_url(store_url)  # use the same normalization helper get_item_urls_from_store uses
    session = _session or build_session(apply_proxy_country(proxy_url, base))
    collected: dict[str, tuple[str, float | None, str]] = {}
    page = 1
    while page <= max_pages:
        html = session.get(_page_url(base, page), timeout=30).text
        if is_challenge_page(html):
            raise ChallengeError(f"challenge on listing page {page}, gathered {len(collected)}")
        cards = parse_listing_cards(html)
        if not cards:
            break
        for item_id, price, url in cards:
            collected.setdefault(item_id, (item_id, price, url))
        if not _has_next_page(html):
            break
        page += 1
    return list(collected.values())
```

(If `_normalize_store_url` does not exist under that name, reuse whatever normalization `get_item_urls_from_store` performs inline; extract it into a shared helper as part of this task so both functions share it. Keep `build_session`, `apply_proxy_country`, `ChallengeError` imports consistent with the file.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_store_listings.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scraper/store.py tests/test_store_listings.py
git commit -m "feat: get_store_listings paginated id+price crawl for delta"
```

### Task E3: Pure delta diff

**Files:**
- Create: `scraper/delta.py`, `tests/test_delta.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_delta.py
from scraper.delta import compute_delta


def test_compute_delta_new_changed_missing():
    listings = [
        ("111111111111", 10.0),   # unchanged
        ("222222222222", 25.0),   # price changed
        ("333333333333", 5.0),    # new
    ]
    db_prices = {
        "111111111111": 10.0,
        "222222222222": 20.0,
        "444444444444": 8.0,      # missing from store now
    }
    to_fetch, to_deactivate = compute_delta(listings, db_prices)
    assert sorted(to_fetch) == ["222222222222", "333333333333"]
    assert to_deactivate == ["444444444444"]


def test_compute_delta_none_price_is_not_a_change():
    listings = [("111111111111", None)]
    db_prices = {"111111111111": 10.0}
    to_fetch, to_deactivate = compute_delta(listings, db_prices)
    assert to_fetch == []  # unknown price is not treated as changed
    assert to_deactivate == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_delta.py -v`
Expected: FAIL (`scraper.delta` missing)

- [ ] **Step 3: Implement `compute_delta`**

```python
# scraper/delta.py
"""Pure delta computation: compare current store listings against DB state."""


def compute_delta(
    listings: list[tuple[str, float | None]],
    db_prices: dict[str, float],
) -> tuple[list[str], list[str]]:
    """Return (item_ids_to_fetch, item_ids_to_deactivate).

    Fetch: items not in the DB, or whose listing price differs from the stored
    price. A None listing price means "price not shown on the card" and is never
    treated as a change. Deactivate: DB items no longer present in the store.
    """
    to_fetch: list[str] = []
    listing_ids: set[str] = set()
    for item_id, price in listings:
        listing_ids.add(item_id)
        if item_id not in db_prices:
            to_fetch.append(item_id)
        elif price is not None and abs(price - db_prices[item_id]) > 0.001:
            to_fetch.append(item_id)
    to_deactivate = [item_id for item_id in db_prices if item_id not in listing_ids]
    return to_fetch, to_deactivate
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_delta.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scraper/delta.py tests/test_delta.py
git commit -m "feat: pure compute_delta diff for new/changed/missing items"
```

### Task E4: Async DB delta reads

**Files:**
- Modify: `scraper/db.py`
- Test: `tests/test_db.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db.py (add)
import pytest
from scraper.db import insert_product, get_store_item_prices, mark_items_inactive
# reuse the existing make_record helper in this test file


@pytest.mark.asyncio
async def test_store_item_prices_and_deactivate(db_pool):
    await insert_product(db_pool, make_record(item_id="111111111111", price=10.0, store_url="https://s"))
    await insert_product(db_pool, make_record(item_id="222222222222", price=20.0, store_url="https://s"))
    prices = await get_store_item_prices(db_pool, "https://s")
    assert prices == {"111111111111": 10.0, "222222222222": 20.0}

    n = await mark_items_inactive(db_pool, "https://s", ["222222222222"])
    assert n == 1
    prices_after = await get_store_item_prices(db_pool, "https://s")
    assert "222222222222" not in prices_after  # inactive excluded
```

(If `make_record` does not accept `store_url`/`price` kwargs, extend it minimally in the test file.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_db.py::test_store_item_prices_and_deactivate -v`
Expected: FAIL (functions missing)

- [ ] **Step 3: Implement the two async functions**

Add to `scraper/db.py`:

```python
async def get_store_item_prices(pool: asyncpg.Pool, store_url: str) -> dict[str, float]:
    rows = await pool.fetch(
        "SELECT item_id, price FROM products WHERE store_url = $1 AND is_active = true",
        store_url,
    )
    return {r["item_id"]: float(r["price"]) for r in rows if r["price"] is not None}


async def mark_items_inactive(pool: asyncpg.Pool, store_url: str, item_ids: list[str]) -> int:
    if not item_ids:
        return 0
    result = await pool.execute(
        "UPDATE products SET is_active = false WHERE store_url = $1 AND item_id = ANY($2::text[])",
        store_url, item_ids,
    )
    return int(result.split()[-1])  # "UPDATE <n>"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_db.py::test_store_item_prices_and_deactivate -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scraper/db.py tests/test_db.py
git commit -m "feat: async DB reads for delta (store prices, mark inactive)"
```

### Task E5: `scrape delta` CLI command

**Files:**
- Modify: `scraper/cli.py` (the `scrape` group, near line 482)
- Test: manual (covered by Phase I E2E); add a thin import-smoke test

- [ ] **Step 1: Implement the command**

Add under the `scrape` group in `scraper/cli.py`, mirroring how `scrape start` builds its async pool and reads stores:

```python
@scrape.command("delta")
@click.option("--niche", default=None, help="Only delta-scan stores with this niche tag.")
def scrape_delta(niche):
    """Re-scan store listing pages and queue only new or price-changed items."""
    import asyncio
    from scraper.config import Settings
    from scraper.store import get_store_listings
    from scraper.delta import compute_delta
    from scraper.queue import get_redis, get_queue, enqueue_items, mark_item_queued
    from scraper import db as dbmod
    import asyncpg

    settings = Settings()
    proxy = _resolve_proxy(settings)  # use the same proxy-resolution the worker/CLI already uses

    async def run():
        pool = await asyncpg.create_pool(settings.database_url)
        try:
            stores = await dbmod.list_stores(pool)
            redis_conn = get_redis(settings.redis_url)
            queue = get_queue(redis_conn)
            for store in stores:
                if niche and store["niche"] != niche:
                    continue
                store_url = store["store_url"]
                listings = get_store_listings(store_url, proxy_url=proxy)
                db_prices = await dbmod.get_store_item_prices(pool, store_url)
                to_fetch, to_deactivate = compute_delta(
                    [(i, p) for (i, p, _u) in listings], db_prices
                )
                url_by_id = {i: u for (i, _p, u) in listings}
                fetch_urls = [url_by_id[i] for i in to_fetch]
                # bypass dedup set so changed items re-queue
                for i in to_fetch:
                    mark_item_queued(redis_conn, i)
                queued = 0
                for j in range(0, len(fetch_urls), settings.batch_size):
                    batch = fetch_urls[j : j + settings.batch_size]
                    from scraper.worker import scrape_batch
                    queue.enqueue(scrape_batch, batch, store["niche"], store_url, 0, job_timeout=600)
                    queued += len(batch)
                deactivated = await dbmod.mark_items_inactive(pool, store_url, to_deactivate)
                click.echo(f"{store_url}: {queued} queued, {deactivated} deactivated")
        finally:
            await pool.close()

    asyncio.run(run())
```

(Use the existing proxy-resolution helper in `cli.py`; if none, read `PROXY_REDIS_KEY` from Redis then fall back to `settings.proxy_url`, matching `worker._get_proxy_url`.)

- [ ] **Step 2: Smoke test the CLI wiring**

Run: `python -c "from scraper.cli import cli; cli(['scrape', '--help'])" 2>&1 | grep delta`
Expected: `delta` listed as a subcommand.

- [ ] **Step 3: Commit**

```bash
git add scraper/cli.py
git commit -m "feat: scrape delta command (listing diff, selective requeue, deactivate)"
```

---

## Phase F: Large-store partitioning

### Task F1: Price-range partitioning past the browse ceiling

**Files:**
- Modify: `scraper/store.py`
- Test: `tests/test_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_store.py (add)
from scraper.store import _price_partitions


def test_price_partitions_splits_range():
    parts = _price_partitions(0, 1000, splits=4)
    assert parts == [(0, 250), (250, 500), (500, 750), (750, 1000)]


def test_price_partition_url_params():
    from scraper.store import _partition_url
    u = _partition_url("https://www.ebay.com.au/str/x", 250, 500)
    assert "_udlo=250" in u and "_udhi=500" in u
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_store.py::test_price_partitions_splits_range -v`
Expected: FAIL (helpers missing)

- [ ] **Step 3: Implement partition helpers + ceiling-triggered partitioned crawl**

Add to `scraper/store.py`:

```python
_BROWSE_CEILING = 10000  # eBay stops paginating store/search browse around here


def _price_partitions(lo: float, hi: float, splits: int = 4) -> list[tuple[float, float]]:
    step = (hi - lo) / splits
    return [(round(lo + step * i, 2), round(lo + step * (i + 1), 2)) for i in range(splits)]


def _partition_url(base_store_url: str, lo: float, hi: float) -> str:
    sep = "&" if "?" in base_store_url else "?"
    return f"{base_store_url}{sep}_udlo={lo}&_udhi={hi}"
```

Then extend `get_item_urls_from_store`: when a crawl ends with `len(urls)` at or above `_BROWSE_CEILING` (ceiling hit, more items likely hidden), re-crawl across `_price_partitions(0, <max observed price or a high bound>)`, union the resulting item IDs, and return the union. For stores under the ceiling (the common case, e.g. watchadoption), behaviour is unchanged. Keep this opt-in and bounded: one level of partitioning, no recursion.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_store.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scraper/store.py tests/test_store.py
git commit -m "feat: price-range partitioning for stores past eBay's browse ceiling"
```

### Task F2: Full suite green

- [ ] **Step 1: Run the whole suite**

Run: `docker exec ebay-scraper-postgres-1 psql -U scraper -c "DROP DATABASE IF EXISTS ebayscraper_test;" -c "CREATE DATABASE ebayscraper_test;" ; pytest -q`
Expected: all tests pass (original + new). Fix any regressions before proceeding. Do not move to deployment with a red suite.

- [ ] **Step 2: Commit any fixes**

```bash
git add -A && git commit -m "test: full suite green for batch + delta redesign"
```

---

## Phase G: Deploy mechanism (one managed worker per box)

### Task G1: systemd unit + deploy script

**Files:**
- Create: `deploy/ebay-worker.service`
- Modify: `README.md` (deploy section, Phase J)

- [ ] **Step 1: Write the systemd unit**

```ini
# deploy/ebay-worker.service
[Unit]
Description=eBay scraper rq worker (one per box; per-IP rate is governed in-process)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/root/ebay-scraper/.env
ExecStart=/usr/local/bin/scraper worker start
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Document the deploy loop (used live in Phase H)**

Deploy = rsync local repo to the VPS, reinstall editable, restart the single managed worker:

```bash
rsync -az --delete --exclude '.git' --exclude '__pycache__' --exclude '.venv' \
  /Users/fabienbrocklesby/code/general-projects/ebay-scraper/ \
  automation-management:/root/ebay-scraper/
ssh automation-management 'cd /root/ebay-scraper && python3.11 -m pip install -e . --quiet && \
  cp deploy/ebay-worker.service /etc/systemd/system/ && systemctl daemon-reload'
```

- [ ] **Step 3: Commit**

```bash
git add deploy/ebay-worker.service
git commit -m "feat: systemd unit for a single managed worker per box"
```

---

## Phase H: Real infra E2E validation (the handoff gate)

> This phase runs against live eBay, the real VPS, and the residential proxy. It is the acceptance gate for handing to Kieran. Do not skip or mock. Each task has explicit pass criteria.

**Prerequisite checkpoint (ask Fabien):** obtain the IPRoyal residential proxy URL. It will be set via `scraper proxy set <url>` in Task H3. Confirm the Mac's Tailscale IP and that the VPS `.env` `REDIS_URL`/`DATABASE_URL` point at it.

### Task H1: Deploy the branch to the VPS and reduce to one worker

- [ ] **Step 1: Stop the old loose workers**

Run: `ssh automation-management 'pkill -f "scraper worker start"; sleep 2; pgrep -af "scraper worker" || echo "no workers"'`
Expected: `no workers`.

- [ ] **Step 2: Deploy (rsync + reinstall + install unit)** using the Task G1 Step 2 commands.

- [ ] **Step 3: Start exactly one managed worker**

Run: `ssh automation-management 'systemctl enable --now ebay-worker && sleep 3 && systemctl is-active ebay-worker && pgrep -afc "scraper worker start"'`
Expected: `active` and a process count of `1`.

### Task H2: Direct-IP backfill on a real random store (no proxy)

- [ ] **Step 1: Pick a fresh random store** (not watchadoption). Browse eBay and choose any active store URL, e.g. an `ebay.com.au/str/<name>`. Record it.

- [ ] **Step 2: Clear proxy so the VPS uses its own IP**

Run: `scraper proxy clear; scraper proxy status`
Expected: status shows no proxy set.

- [ ] **Step 3: Add the store and start the backfill**

Run:
```bash
scraper store add "<STORE_URL>" --niche e2e
scraper scrape start --niche e2e
```
Expected: prints `N items found, N new jobs queued`.

- [ ] **Step 4: Watch throughput on the VPS**

Run: `ssh automation-management 'journalctl -u ebay-worker -f --since "2 min ago"'` for ~60s, and separately:
Run: `watch -n5 'docker exec ebay-scraper-postgres-1 psql -U scraper -d ebayscraper -tc "SELECT count(*) FROM products WHERE niche=\\'e2e\\'"'`
Expected: row count climbs at **several items/sec**, far above the old ~2/min. Record the items/sec over a 2-minute window.

- [ ] **Step 5: Pass criteria**
  - Throughput >= 2.5 items/sec on one box at default `WORKER_CONCURRENCY=8`, `MAX_RPS_PER_IP=6` (2 requests/item, so ~3 items/s is the ceiling at this cap, ~130x the old rate). Only after Task H3 proves residential escalation absorbs challenges, optionally raise `MAX_RPS_PER_IP`/`WORKER_CONCURRENCY` via the VPS `.env` + `systemctl restart ebay-worker` to push higher; record the final values.
  - Challenge rate low enough that the backfill completes without stalling.
  - Spot-check 5 rows: `scraper export --niche e2e --output /tmp/e2e.csv` then inspect that title, price, currency, description, item_specifics, mpn/upc are populated for items that have them.

### Task H3: Proxy escalation works

- [ ] **Step 1: Set the residential proxy**

Run: `scraper proxy set "<IPROYAL_URL>"; scraper proxy test`
Expected: proxy test fetches a sample item successfully through residential.

- [ ] **Step 2: Force escalation pressure** by temporarily raising concurrency/rate on the VPS to provoke challenges:

Run: `ssh automation-management 'sed -i "s/^MAX_RPS_PER_IP=.*/MAX_RPS_PER_IP=30/" /root/ebay-scraper/.env; sed -i "s/^WORKER_CONCURRENCY=.*/WORKER_CONCURRENCY=40/" /root/ebay-scraper/.env; systemctl restart ebay-worker'`

- [ ] **Step 3: Run a backfill on another fresh store and observe**

Expected: when the box IP starts getting challenged, `BoxProxyState` flips to residential (challenge errors do not turn into lost items: items still land in the DB via the residential retry). Verify no sustained job failures and the DB keeps filling. Then restore safe `.env` values and `systemctl restart ebay-worker`.

- [ ] **Step 4: Pass criteria:** with the proxy set, an aggressive run still completes and stores items (escalation absorbs the challenges rather than dropping items).

### Task H4: Delta mode is cheap and correct

- [ ] **Step 1: Run a delta immediately after backfill**

Run: `scraper scrape delta --niche e2e`
Expected: `0 queued` (or near-zero) and `0 deactivated`, because nothing changed since backfill. This proves delta does not re-fetch unchanged items.

- [ ] **Step 2: Simulate a change** by manually editing one row's price down in the DB, then re-run delta:

Run:
```bash
docker exec ebay-scraper-postgres-1 psql -U scraper -d ebayscraper -c \
  "UPDATE products SET price = price - 1 WHERE niche='e2e' AND item_id = (SELECT item_id FROM products WHERE niche='e2e' LIMIT 1)"
scraper scrape delta --niche e2e
```
Expected: `1 queued` (the price now differs from the live listing, so it re-fetches). Confirms change detection.

- [ ] **Step 3: Pass criteria:** delta with no real changes queues ~0; an induced price difference queues exactly the affected item; items removed from the store get `is_active=false`.

### Task H5: Multi-store soak across random niches

- [ ] **Step 1: Add 3-5 unrelated random stores** across different categories (watches, car parts, clothing, etc.), each with its own `--niche`.

- [ ] **Step 2: Backfill all, then export per niche** and sanity-check counts and field completeness.

- [ ] **Step 3: Pass criteria:** all stores backfill to completion on one box inside the budgeted rate without manual babysitting; exports look correct; no crashes in `journalctl -u ebay-worker`.

---

## Phase I: Docs + handoff

### Task I1: Update README for the new model

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Document**
  - `scrape start` = backfill; `scrape delta` = cheap re-scan (what it queues, what it deactivates).
  - One worker process per box; scale by adding boxes. Per-box knobs: `WORKER_CONCURRENCY`, `MAX_RPS_PER_IP`, `BATCH_SIZE`, `CHALLENGE_ESCALATION_THRESHOLD`, `CHALLENGE_COOLDOWN_SECONDS`, with the validated safe defaults and how to tune them from observed challenge rate.
  - Cheap-IP-first + residential-as-fallback: set the proxy with `scraper proxy set`; it is only used on challenge.
  - systemd deploy (`deploy/ebay-worker.service`) and the rsync deploy loop.
  - Recommended operating rhythm for Kieran: backfill each store once, then run `scrape delta` on a schedule (cron) for cheap upkeep.

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: document batch/delta model, per-box tuning, and deploy"
```

### Task I2: Finish the branch

- [ ] **Step 1:** Run the full suite once more green (`pytest -q`), confirm Phase H pass criteria are all recorded, then use superpowers:finishing-a-development-branch to merge/PR.

---

## Self-review notes (author check, done)

- **Spec coverage:** concurrency (Phase A/C), cheap-IP-first + residential fallback (A1 BoxProxyState, C1/C2), backfill+delta (D, E), large-store splitting (F), per-IP rate (A1 TokenBucket), bulk writes (B2), real E2E on VPS+proxy+random stores (H). All spec sections map to tasks.
- **Type consistency:** `scrape_batch(item_urls, niche, store_url, attempt)`, `_scrape_one(url, residential_proxy, box_state, bucket)`, `_bulk_upsert(database_url, products, niche, store_url)`, `compute_delta(listings, db_prices) -> (to_fetch, to_deactivate)`, `parse_listing_cards -> list[(id, price, url)]`, `get_store_listings -> list[(id, price, url)]` are consistent across tasks.
- **Placeholders:** none. Two tasks (E2 `_normalize_store_url`, E5 proxy-resolution) explicitly say "reuse the existing helper, extract if absent" rather than inventing a name, because the exact local helper name must be confirmed against `store.py`/`cli.py` at edit time. Real markup for the listing parser is captured as a fixture (E1 Step 1) rather than guessed.
```

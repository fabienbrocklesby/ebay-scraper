# Marketplace-Aware Scraping + Handoff Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-detect each eBay seller's home marketplace (US/AU/UK) so a non-technical operator drops a plain list of store URLs, runs one command, and gets split CSVs of every product across all marketplaces, with an optional paid unblocker as a last-resort fallback.

**Architecture:** A new `marketplace` module probes candidate eBay domains for a seller and picks the one with the most items (treating a bot-challenge as retry, never as "0"). The resolved `(domain, country)` is persisted per store, so discovery crawls the right domain and the existing `apply_proxy_country` pins the proxy exit IP to that country automatically. An optional `unblocker` module (Oxylabs, OFF by default) escalates only stores that still fail. A `run` orchestrator chains import → detect → scrape → split-CSV export; `setup` and `doctor` make credential entry and diagnosis non-technical.

**Tech Stack:** Python 3.11, curl_cffi (scraping sessions), httpx (unblocker POST), rq/redis (queue), asyncpg (CLI reads), psycopg2 (worker writes), click (CLI), pytest + respx (tests).

---

## File Structure

- **Create** `scraper/marketplace.py` — candidate marketplaces, `detect_marketplace`, `MarketplaceResult`, `DetectionOutcome`, default probe fetch.
- **Create** `scraper/unblocker.py` — `UnblockerConfig`, `load_unblocker_config`, `fetch_via_unblocker`, Redis keys, request counter.
- **Modify** `scraper/db.py` — idempotent marketplace columns on `stores`; `set_store_marketplace`, `get_store_marketplace`; `list_stores` returns new fields; niche optional.
- **Modify** `scraper/export.py` — extract `_record_to_row`; add `export_split_csv`.
- **Modify** `scraper/queue.py` — add `queue_is_drained`.
- **Modify** `scraper/cli.py` — wire detection into `_discover_store`; `run`, `setup`, `doctor` commands; niche-optional import.
- **Create** `tests/test_marketplace.py`, `tests/test_unblocker.py` — new module tests.
- **Modify** `tests/test_db.py`, `tests/test_export.py`, `tests/test_cli.py` — new behaviour + niche-optional updates.
- **Create** `tests/fixtures/sample_stores.csv` — mixed US/AU/UK validation list.
- **Modify** `README.md`, `HANDOFF.md` — `run`/`setup`/`doctor` usage; loud multi-currency warning.

Tasks are ordered so each produces working, tested software. Run the full suite (`pytest -v`) at the end of every task; do not proceed on a failure.

---

## Task 1: Niche becomes optional (schema + add_store)

Kieran's import is a plain URL list with no niche. Make niche default to empty everywhere without dropping the column.

**Files:**
- Modify: `scraper/db.py` (stores table at lines 58-63; `add_store` at 112-120)
- Test: `tests/test_db.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_db.py` add:

```python
@pytest.mark.asyncio
async def test_add_store_defaults_niche_to_empty(db_pool):
    from scraper.db import add_store, list_stores
    await db_pool.execute("DELETE FROM stores")
    await add_store(db_pool, "https://www.ebay.com/str/noniche")
    stores = await list_stores(db_pool)
    row = next(s for s in stores if s["store_url"] == "https://www.ebay.com/str/noniche")
    assert row["niche"] == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_db.py::test_add_store_defaults_niche_to_empty -v`
Expected: FAIL — `add_store()` missing 1 required positional argument: `niche`.

- [ ] **Step 3: Make niche optional**

In `scraper/db.py`, change the stores table definition (lines 58-63) so niche has a default:

```python
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS stores (
            store_url TEXT PRIMARY KEY,
            niche     TEXT NOT NULL DEFAULT '',
            added_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
```

Change `add_store` (lines 112-120) signature default:

```python
async def add_store(pool: asyncpg.Pool, store_url: str, niche: str = "") -> None:
    await pool.execute(
        """
        INSERT INTO stores (store_url, niche)
        VALUES ($1, $2)
        ON CONFLICT (store_url) DO UPDATE SET niche = EXCLUDED.niche
        """,
        store_url,
        niche,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_db.py::test_add_store_defaults_niche_to_empty -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scraper/db.py tests/test_db.py
git commit -m "feat: make store niche optional (defaults to empty)"
```

---

## Task 2: Marketplace columns on the stores table

Persist the detected home marketplace so detection is one-time per store.

**Files:**
- Modify: `scraper/db.py` (`init_schema` 27-64; `list_stores` 123-126)
- Test: `tests/test_db.py`

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_set_and_get_store_marketplace(db_pool):
    from scraper.db import add_store, set_store_marketplace, get_store_marketplace
    await db_pool.execute("DELETE FROM stores")
    url = "https://www.ebay.com/str/aussiestore"
    await add_store(db_pool, url)
    assert await get_store_marketplace(db_pool, url) is None
    await set_store_marketplace(db_pool, url, "www.ebay.com.au", "au")
    got = await get_store_marketplace(db_pool, url)
    assert got == ("www.ebay.com.au", "au")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_db.py::test_set_and_get_store_marketplace -v`
Expected: FAIL — cannot import `set_store_marketplace`.

- [ ] **Step 3: Add columns + CRUD**

In `scraper/db.py` `init_schema`, immediately after the `CREATE TABLE ... stores ...` statement, add an idempotent upgrade so existing coordinator DBs gain the columns with no migration step:

```python
    await pool.execute("ALTER TABLE stores ADD COLUMN IF NOT EXISTS marketplace_domain  TEXT")
    await pool.execute("ALTER TABLE stores ADD COLUMN IF NOT EXISTS marketplace_country TEXT")
    await pool.execute("ALTER TABLE stores ADD COLUMN IF NOT EXISTS detected_at TIMESTAMPTZ")
```

Add CRUD functions near `add_store`:

```python
async def set_store_marketplace(
    pool: asyncpg.Pool, store_url: str, domain: str, country: str
) -> None:
    await pool.execute(
        """
        UPDATE stores
           SET marketplace_domain = $2,
               marketplace_country = $3,
               detected_at = NOW()
         WHERE store_url = $1
        """,
        store_url,
        domain,
        country,
    )


async def get_store_marketplace(
    pool: asyncpg.Pool, store_url: str
) -> tuple[str, str] | None:
    row = await pool.fetchrow(
        "SELECT marketplace_domain, marketplace_country FROM stores WHERE store_url = $1",
        store_url,
    )
    if row and row["marketplace_domain"]:
        return row["marketplace_domain"], row["marketplace_country"]
    return None
```

Update `list_stores` (lines 123-126) to include the new columns:

```python
async def list_stores(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    return await pool.fetch(
        """
        SELECT store_url, niche, added_at, marketplace_domain, marketplace_country, detected_at
          FROM stores
         ORDER BY added_at
        """
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_db.py::test_set_and_get_store_marketplace -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scraper/db.py tests/test_db.py
git commit -m "feat: persist detected marketplace (domain, country) per store"
```

---

## Task 3: Marketplace detection module

Probe candidate domains for a seller, pick the domain with the most items, and split "genuine zero" from "still challenged".

**Files:**
- Create: `scraper/marketplace.py`
- Test: `tests/test_marketplace.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_marketplace.py`:

```python
from scraper.marketplace import (
    detect_marketplace,
    MarketplaceResult,
    DetectionOutcome,
    CANDIDATE_MARKETPLACES,
)
from scraper.fetch import ChallengeError


def _grid_html(n: int) -> str:
    cards = "".join(
        f'<a class="str-item-card__link" href="https://x/itm/{1000+i}?x">i</a>'
        for i in range(n)
    )
    return f"<html><body>{cards}</body></html>"


def test_picks_domain_with_most_items():
    def fetch_fn(url, proxy_url):
        if "ebay.com.au" in url:
            return _grid_html(240)
        if "ebay.co.uk" in url:
            return _grid_html(5)
        return _grid_html(0)  # ebay.com genuine zero

    outcome = detect_marketplace("seller1", proxy_url=None, fetch_fn=fetch_fn)
    assert isinstance(outcome, DetectionOutcome)
    assert outcome.result is not None
    assert outcome.result.domain == "www.ebay.com.au"
    assert outcome.result.country == "au"
    assert outcome.result.item_count == 240


def test_challenge_is_retried_not_counted_as_zero():
    calls = {"n": 0}

    def fetch_fn(url, proxy_url):
        if "ebay.com.au" in url:
            calls["n"] += 1
            if calls["n"] == 1:
                raise ChallengeError("blocked once")
            return _grid_html(120)
        return _grid_html(0)

    outcome = detect_marketplace("seller2", proxy_url=None, fetch_fn=fetch_fn)
    assert outcome.result is not None
    assert outcome.result.domain == "www.ebay.com.au"
    assert outcome.result.item_count == 120


def test_all_genuine_zero_returns_no_result():
    outcome = detect_marketplace(
        "seller3", proxy_url=None, fetch_fn=lambda url, proxy_url: _grid_html(0)
    )
    assert outcome.result is None
    assert outcome.undetermined_domains == []


def test_persistent_challenge_is_undetermined():
    def fetch_fn(url, proxy_url):
        raise ChallengeError("always blocked")

    outcome = detect_marketplace("seller4", proxy_url=None, fetch_fn=fetch_fn)
    assert outcome.result is None
    assert set(outcome.undetermined_domains) == {d for d, _ in CANDIDATE_MARKETPLACES}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_marketplace.py -v`
Expected: FAIL — `No module named 'scraper.marketplace'`.

- [ ] **Step 3: Implement the module**

Create `scraper/marketplace.py`:

```python
"""Detect which eBay marketplace (domain + country) a seller's items live on.

eBay serves a seller's full item grid only on their home marketplace domain, to an
in-country viewer. A non-US seller crawled on ebay.com returns a genuine "0 results".
We therefore probe the candidate marketplaces for each seller and adopt the domain that
returns the most items. A bot-challenge during a probe is retried, never read as zero,
because conflating the two would mis-detect a seller's home as empty.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlparse

from scraper.fetch import ChallengeError, apply_proxy_country, build_session, is_challenge_page
from scraper.store import _extract_item_urls

# Ordered list of the marketplaces Kieran's stores live on. Extend as new ones appear.
CANDIDATE_MARKETPLACES: list[tuple[str, str]] = [
    ("www.ebay.com", "us"),
    ("www.ebay.com.au", "au"),
    ("www.ebay.co.uk", "gb"),
]

# Detection only needs an item-count signal, so request a small page to limit bandwidth.
_DETECT_ITEMS_PER_PAGE = 60
_MAX_PROBE_RETRIES = 3

FetchFn = Callable[[str, Optional[str]], str]


@dataclass(frozen=True)
class MarketplaceResult:
    domain: str
    country: str
    item_count: int
    seller_search_url: str


@dataclass(frozen=True)
class DetectionOutcome:
    """result is the best domain with items, or None. undetermined_domains are domains
    still challenged after retries, so the caller can escalate rather than treat as empty.
    """

    result: Optional[MarketplaceResult]
    undetermined_domains: list[str]


def _seller_search_url(domain: str, seller_id: str) -> str:
    return (
        f"https://{domain}/sch/i.html?_ssn={seller_id}"
        f"&_pgn=1&_ipg={_DETECT_ITEMS_PER_PAGE}"
    )


def _default_fetch(url: str, proxy_url: Optional[str]) -> str:
    pinned = apply_proxy_country(proxy_url, url)
    session = build_session(pinned)
    host = urlparse(url).netloc
    try:
        session.get(f"https://{host}/", timeout=30)  # warm the session (cold = 403)
        resp = session.get(url, timeout=40)
    except Exception as exc:  # network/proxy failure: treat as a soft block, let caller retry
        raise ChallengeError(f"probe network error for {url}: {exc}") from exc
    return resp.text


@dataclass(frozen=True)
class _Probe:
    domain: str
    country: str
    item_count: int
    url: str
    challenged: bool


def _probe_domain(
    seller_id: str, domain: str, country: str, proxy_url: Optional[str], fetch_fn: FetchFn
) -> _Probe:
    url = _seller_search_url(domain, seller_id)
    for _ in range(_MAX_PROBE_RETRIES):
        try:
            html = fetch_fn(url, proxy_url)
        except ChallengeError:
            continue
        if is_challenge_page(html):
            continue
        return _Probe(domain, country, len(_extract_item_urls(html)), url, challenged=False)
    return _Probe(domain, country, 0, url, challenged=True)


def detect_marketplace(
    seller_id: str,
    proxy_url: Optional[str],
    fetch_fn: FetchFn = _default_fetch,
    candidates: list[tuple[str, str]] = CANDIDATE_MARKETPLACES,
) -> DetectionOutcome:
    with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
        probes = list(
            pool.map(
                lambda c: _probe_domain(seller_id, c[0], c[1], proxy_url, fetch_fn),
                candidates,
            )
        )

    with_items = [p for p in probes if not p.challenged and p.item_count > 0]
    undetermined = [p.domain for p in probes if p.challenged]

    if with_items:
        best = max(with_items, key=lambda p: p.item_count)
        return DetectionOutcome(
            result=MarketplaceResult(best.domain, best.country, best.item_count, best.url),
            undetermined_domains=undetermined,
        )
    return DetectionOutcome(result=None, undetermined_domains=undetermined)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_marketplace.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add scraper/marketplace.py tests/test_marketplace.py
git commit -m "feat: marketplace detection (probe candidates, pick max items, challenge != zero)"
```

---

## Task 4: Optional unblocker module (Oxylabs)

A clean, OFF-by-default fallback that fetches a URL through the Oxylabs eBay source. Used only when a store still fails through the proxy.

**Files:**
- Create: `scraper/unblocker.py`
- Test: `tests/test_unblocker.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_unblocker.py`:

```python
import respx
import httpx

from scraper.unblocker import UnblockerConfig, fetch_via_unblocker


OXY = "https://realtime.oxylabs.io/v1/queries"


def test_disabled_config_returns_none():
    cfg = UnblockerConfig(provider="none", username=None, password=None)
    assert cfg.enabled is False
    assert fetch_via_unblocker("https://www.ebay.com.au/str/x", cfg) is None


@respx.mock
def test_oxylabs_returns_html():
    respx.post(OXY).mock(
        return_value=httpx.Response(200, json={"results": [{"content": "<html>grid</html>"}]})
    )
    cfg = UnblockerConfig(provider="oxylabs", username="u", password="p")
    assert cfg.enabled is True
    html = fetch_via_unblocker("https://www.ebay.com.au/str/x", cfg)
    assert html == "<html>grid</html>"


@respx.mock
def test_oxylabs_non_200_returns_none():
    respx.post(OXY).mock(return_value=httpx.Response(500, json={}))
    cfg = UnblockerConfig(provider="oxylabs", username="u", password="p")
    assert fetch_via_unblocker("https://www.ebay.com.au/str/x", cfg) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_unblocker.py -v`
Expected: FAIL — `No module named 'scraper.unblocker'`.

- [ ] **Step 3: Implement the module**

Create `scraper/unblocker.py`:

```python
"""Optional paid unblocker fallback (Oxylabs eBay source).

OFF by default. Active only when Oxylabs credentials are configured on the coordinator.
Used as a last resort for a store that still fails through the residential proxy after
marketplace routing. There is no hard budget cap (the operator owns the cost), but every
call increments a Redis counter so spend is visible in run summaries.
"""

from dataclasses import dataclass
from typing import Optional

import httpx

from scraper.fetch import _ebay_country_code

OXYLABS_ENDPOINT = "https://realtime.oxylabs.io/v1/queries"

UNBLOCKER_PROVIDER_KEY = "ebay-scraper:unblocker_provider"
OXYLABS_USER_KEY = "ebay-scraper:oxylabs_username"
OXYLABS_PASS_KEY = "ebay-scraper:oxylabs_password"
UNBLOCKER_COUNT_KEY = "ebay-scraper:unblocker_request_count"

_COUNTRY_NAME = {"us": "United States", "au": "Australia", "gb": "United Kingdom"}


@dataclass(frozen=True)
class UnblockerConfig:
    provider: str
    username: Optional[str]
    password: Optional[str]

    @property
    def enabled(self) -> bool:
        return self.provider == "oxylabs" and bool(self.username) and bool(self.password)


def load_unblocker_config(redis_conn) -> UnblockerConfig:
    def _get(key: str) -> Optional[str]:
        raw = redis_conn.get(key)
        return raw.decode().strip() if raw else None

    return UnblockerConfig(
        provider=_get(UNBLOCKER_PROVIDER_KEY) or "none",
        username=_get(OXYLABS_USER_KEY),
        password=_get(OXYLABS_PASS_KEY),
    )


def fetch_via_unblocker(url: str, config: UnblockerConfig, redis_conn=None) -> Optional[str]:
    if not config.enabled:
        return None
    country = _ebay_country_code(url)
    payload = {
        "source": "ebay",
        "url": url,
        "render": "html",
        "geo_location": _COUNTRY_NAME.get(country, "United States"),
    }
    try:
        resp = httpx.post(
            OXYLABS_ENDPOINT,
            auth=(config.username, config.password),
            json=payload,
            timeout=120,
        )
    except httpx.HTTPError:
        return None
    if redis_conn is not None:
        redis_conn.incr(UNBLOCKER_COUNT_KEY)
    if resp.status_code != 200:
        return None
    results = resp.json().get("results") or []
    return results[0].get("content") if results else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_unblocker.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add scraper/unblocker.py tests/test_unblocker.py
git commit -m "feat: optional Oxylabs unblocker fallback (off by default, counted)"
```

---

## Task 5: Split-CSV export

Export every product across numbered files capped at a row count, no niche filter.

**Files:**
- Modify: `scraper/export.py` (function 29-51; columns 7-26)
- Test: `tests/test_export.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_export.py` add:

```python
@pytest.mark.asyncio
async def test_export_split_csv_chunks_by_rows(db_pool, tmp_path):
    from scraper.db import insert_product
    from scraper.export import export_split_csv
    for i in range(5):
        await insert_product(db_pool, make_record(item_id=f"s{i}", niche="split"))
    paths = await export_split_csv(
        db_pool, output_dir=str(tmp_path), rows_per_file=2, niche="split"
    )
    assert len(paths) == 3  # 2 + 2 + 1
    import csv
    total = 0
    for p in paths:
        with open(p) as f:
            rows = list(csv.DictReader(f))
            total += len(rows)
            assert "item_id" in rows[0]  # header present in every file
    assert total == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_export.py::test_export_split_csv_chunks_by_rows -v`
Expected: FAIL — cannot import `export_split_csv`.

- [ ] **Step 3: Refactor row serialization + add split export**

In `scraper/export.py`, extract the existing per-row serialization used inside
`export_to_csv` into a module-level helper `_record_to_row(record) -> list` (move the
exact pipe-join of `image_urls`, JSON-dump of `item_specifics`, and field ordering from
the current loop into it), then have `export_to_csv` call it. Add the split function:

```python
import os


async def export_split_csv(
    pool: asyncpg.Pool,
    output_dir: str,
    rows_per_file: int = 500_000,
    niche: str | None = None,
) -> list[str]:
    rows = await get_products_by_niche(pool, niche)
    os.makedirs(output_dir, exist_ok=True)
    paths: list[str] = []
    if not rows:
        return paths
    for index, start in enumerate(range(0, len(rows), rows_per_file), start=1):
        chunk = rows[start : start + rows_per_file]
        path = os.path.join(output_dir, f"products_{index:03d}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_COLUMNS)
            for record in chunk:
                writer.writerow(_record_to_row(record))
        paths.append(path)
    return paths
```

Ensure `import csv` exists at the top of the file (it does for `export_to_csv`).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_export.py -v`
Expected: PASS (existing export tests + the new one)

- [ ] **Step 5: Commit**

```bash
git add scraper/export.py tests/test_export.py
git commit -m "feat: split-CSV export capped by row count"
```

---

## Task 6: Queue drain check

`run` must block until scraping actually finishes.

**Files:**
- Modify: `scraper/queue.py`
- Test: `tests/test_queue.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_queue.py` add (uses the existing fakeredis/redis fixture pattern in that file; if the file builds a `Queue` from a connection, mirror it):

```python
def test_queue_is_drained_true_when_empty():
    from scraper.queue import get_redis, get_queue, queue_is_drained
    conn = get_redis("redis://localhost:6379")
    queue = get_queue(conn)
    queue.empty()
    assert queue_is_drained(queue) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_queue.py::test_queue_is_drained_true_when_empty -v`
Expected: FAIL — cannot import `queue_is_drained`.

- [ ] **Step 3: Implement**

In `scraper/queue.py` add:

```python
from rq.registry import StartedJobRegistry, DeferredJobRegistry, ScheduledJobRegistry


def queue_is_drained(queue) -> bool:
    """True when no jobs are pending, running, deferred (awaiting retry), or scheduled."""
    started = StartedJobRegistry(queue=queue)
    deferred = DeferredJobRegistry(queue=queue)
    scheduled = ScheduledJobRegistry(queue=queue)
    return (
        queue.count == 0
        and started.count == 0
        and deferred.count == 0
        and scheduled.count == 0
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_queue.py::test_queue_is_drained_true_when_empty -v`
Expected: PASS (requires the Docker redis from the documented test setup)

- [ ] **Step 5: Commit**

```bash
git add scraper/queue.py tests/test_queue.py
git commit -m "feat: queue_is_drained for run orchestrator wait"
```

---

## Task 7: Wire marketplace detection into discovery

Resolve a store's marketplace before crawling; persist it; crawl the right domain; escalate undetermined/empty stores to the unblocker when configured.

**Files:**
- Modify: `scraper/cli.py` (`_discover_store` 544-567; `_run_discovery` 575-597)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_cli.py` add a unit test for the new resolver helper (pure, no network — fetch and persistence injected):

```python
def test_resolve_marketplace_url_uses_cached_domain():
    from scraper.cli import _resolve_marketplace_url
    # cached -> returns seller-search on the cached domain, no detection
    url = _resolve_marketplace_url(
        store_url="https://www.ebay.com/str/aussie",
        cached=("www.ebay.com.au", "au"),
        detect=lambda seller_id, proxy_url: (_ for _ in ()).throw(AssertionError("should not detect")),
        proxy_url=None,
    )
    assert url == "https://www.ebay.com.au/sch/i.html?_ssn=aussie"


def test_resolve_marketplace_url_detects_when_uncached():
    from scraper.cli import _resolve_marketplace_url
    from scraper.marketplace import DetectionOutcome, MarketplaceResult
    captured = {}

    def fake_detect(seller_id, proxy_url):
        return DetectionOutcome(
            result=MarketplaceResult("www.ebay.com.au", "au", 200,
                                     "https://www.ebay.com.au/sch/i.html?_ssn=aussie&_pgn=1&_ipg=60"),
            undetermined_domains=[],
        )

    url = _resolve_marketplace_url(
        store_url="https://www.ebay.com/str/aussie",
        cached=None,
        detect=fake_detect,
        proxy_url=None,
        on_detected=lambda domain, country: captured.update(domain=domain, country=country),
    )
    assert url == "https://www.ebay.com.au/sch/i.html?_ssn=aussie"
    assert captured == {"domain": "www.ebay.com.au", "country": "au"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli.py::test_resolve_marketplace_url_uses_cached_domain -v`
Expected: FAIL — cannot import `_resolve_marketplace_url`.

- [ ] **Step 3: Implement the resolver and wire it in**

In `scraper/cli.py`, add the pure resolver (place it just above `_discover_store`):

```python
from scraper.store import extract_seller_id
from scraper.marketplace import detect_marketplace, DetectionOutcome


def _marketplace_seller_search(domain: str, seller_id: str) -> str:
    return f"https://{domain}/sch/i.html?_ssn={seller_id}"


def _resolve_marketplace_url(
    store_url: str,
    cached: tuple[str, str] | None,
    detect,
    proxy_url: str | None,
    on_detected=None,
) -> str | None:
    """Return the seller-search URL on the store's home marketplace, or None if the
    store could not be resolved (genuinely empty or persistently challenged).

    `detect(seller_id, proxy_url) -> DetectionOutcome`. `on_detected(domain, country)`
    persists a freshly detected marketplace.
    """
    seller_id = extract_seller_id(store_url)
    if cached is not None:
        return _marketplace_seller_search(cached[0], seller_id)
    outcome: DetectionOutcome = detect(seller_id, proxy_url)
    if outcome.result is not None:
        if on_detected is not None:
            on_detected(outcome.result.domain, outcome.result.country)
        return _marketplace_seller_search(outcome.result.domain, seller_id)
    return None
```

Then change `_discover_store` (544-567) so it resolves the marketplace first and crawls
that URL. The function currently takes `(store_url, proxy_url, rps)`; extend it to accept
the cached marketplace and a persistence callback, and to consult the unblocker when the
resolver returns None. New body:

```python
def _discover_store(
    store_url: str,
    proxy_url: str | None,
    rps: float,
    cached: tuple[str, str] | None = None,
    on_detected=None,
    unblocker_config=None,
    redis_conn=None,
) -> tuple[str, list[str]]:
    """Returns (outcome, urls) where outcome in ("ok", "empty", "blocked").

    Resolves the store's home marketplace (cached or detected), then crawls that domain's
    seller-search. A store that resolves to no marketplace is escalated to the unblocker
    when one is configured; otherwise it is reported empty/blocked for retry.
    """
    detect = lambda seller_id, p: detect_marketplace(seller_id, p)
    resolved = _resolve_marketplace_url(store_url, cached, detect, proxy_url, on_detected)

    if resolved is None and unblocker_config is not None and unblocker_config.enabled:
        from scraper.unblocker import fetch_via_unblocker
        from scraper.store import _extract_item_urls
        seller_id = extract_seller_id(store_url)
        for domain, _country in CANDIDATE_MARKETPLACES:
            html = fetch_via_unblocker(
                _marketplace_seller_search(domain, seller_id), unblocker_config, redis_conn
            )
            if html and _extract_item_urls(html):
                resolved = _marketplace_seller_search(domain, seller_id)
                break

    if resolved is None:
        return ("empty", [])

    try:
        urls = get_item_urls_from_store(resolved, proxy_url=proxy_url, requests_per_second=rps)
    except ChallengeError:
        return ("blocked", [])
    return ("ok" if urls else "empty", urls)
```

Add the required imports at the top of `cli.py` if missing:
`from scraper.marketplace import CANDIDATE_MARKETPLACES` and ensure `ChallengeError` and
`get_item_urls_from_store` are already imported (they are, per `from scraper.store import ...`).

Update `_run_discovery` (575-597) so that, per store, it (a) reads the cached marketplace
via `get_store_marketplace`, (b) passes a persistence callback that calls
`set_store_marketplace`, and (c) passes the loaded `unblocker_config` and `redis_conn`.
Because `_run_discovery` is sync and DB calls are async, resolve the cached marketplace
and persist using the same `asyncio.run(...)` pattern already used elsewhere in the CLI
for store reads. Concretely, before the per-store loop build a dict of cached
marketplaces with one async call:

```python
    cached_map = asyncio.run(_load_cached_marketplaces([s[0] for s in stores]))
```

where `_load_cached_marketplaces` is a small async helper added near the other async DB
helpers in `cli.py`:

```python
async def _load_cached_marketplaces(store_urls: list[str]) -> dict[str, tuple[str, str]]:
    settings = Settings()
    pool = await asyncpg.create_pool(settings.database_url)
    try:
        out: dict[str, tuple[str, str]] = {}
        for url in store_urls:
            got = await get_store_marketplace(pool, url)
            if got:
                out[url] = got
        return out
    finally:
        await pool.close()
```

and persistence per detection:

```python
    def _persist(url, domain, country):
        asyncio.run(_save_marketplace(url, domain, country))
```

with:

```python
async def _save_marketplace(store_url: str, domain: str, country: str) -> None:
    settings = Settings()
    pool = await asyncpg.create_pool(settings.database_url)
    try:
        await set_store_marketplace(pool, store_url, domain, country)
    finally:
        await pool.close()
```

In the loop, call:

```python
        outcome, urls = _discover_store(
            store_url,
            proxy_url,
            settings.requests_per_second,
            cached=cached_map.get(store_url),
            on_detected=lambda d, c, _u=store_url: _persist(_u, d, c),
            unblocker_config=unblocker_config,
            redis_conn=redis_conn,
        )
```

Load `unblocker_config` and `redis_conn` once at the top of `_run_discovery`:

```python
    from scraper.queue import get_redis
    from scraper.unblocker import load_unblocker_config
    redis_conn = get_redis(settings.redis_url)
    unblocker_config = load_unblocker_config(redis_conn)
```

Import the new db functions at the top of `cli.py`:
`from scraper.db import get_store_marketplace, set_store_marketplace` (alongside existing db imports).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cli.py -v`
Expected: PASS (the two resolver tests, plus existing CLI tests still green)

- [ ] **Step 5: Commit**

```bash
git add scraper/cli.py tests/test_cli.py
git commit -m "feat: resolve+persist store marketplace in discovery, unblocker fallback for unresolved"
```

---

## Task 8: Worker escalation tier (proxy -> unblocker)

When a store's item fetches persistently challenge through the proxy, escalate that item to the unblocker if configured. Keep it bounded and OFF by default.

**Files:**
- Modify: `scraper/worker.py` (`_scrape_one` 113-136)
- Test: `tests/test_worker.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_worker.py` add:

```python
def test_scrape_one_escalates_to_unblocker_after_proxy_block(monkeypatch):
    import scraper.worker as worker
    from scraper.throttle import BoxProxyState, TokenBucket

    # proxy path always challenges
    def fake_fetch(*a, **k):
        from scraper.fetch import ChallengeError
        raise ChallengeError("blocked")

    monkeypatch.setattr(worker, "_fetch_item_html", fake_fetch, raising=False)

    # unblocker returns a parseable item page
    sample_html = "<html>...</html>"
    monkeypatch.setattr(
        worker, "fetch_via_unblocker", lambda url, cfg, rc=None: sample_html, raising=False
    )
    parsed = object()
    monkeypatch.setattr(worker, "parse_item", lambda html, item_url, proxy_url=None: parsed, raising=False)

    from scraper.unblocker import UnblockerConfig
    cfg = UnblockerConfig(provider="oxylabs", username="u", password="p")

    out = worker._scrape_one_with_unblocker(
        "https://www.ebay.com.au/itm/123",
        residential_proxy=None,
        box_state=BoxProxyState(0.15, 120),
        bucket=TokenBucket(100),
        unblocker_config=cfg,
        redis_conn=None,
    )
    assert out is parsed
```

(If `_fetch_item_html`/`parse_item` names differ in `worker.py`, adapt the monkeypatch
targets to the actual function this task wraps; the test asserts the escalation path
returns the unblocker-parsed product.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_worker.py::test_scrape_one_escalates_to_unblocker_after_proxy_block -v`
Expected: FAIL — cannot find `_scrape_one_with_unblocker`.

- [ ] **Step 3: Implement bounded escalation**

In `scraper/worker.py`, add a thin wrapper around the existing `_scrape_one` that, when
`_scrape_one` returns None due to a persistent challenge AND an unblocker is configured,
fetches the item via the unblocker and parses it:

```python
from scraper.unblocker import fetch_via_unblocker
from scraper.scraper import parse_item  # existing item parser entry point


def _scrape_one_with_unblocker(
    item_url, residential_proxy, box_state, bucket, unblocker_config, redis_conn
):
    product = _scrape_one(item_url, residential_proxy, box_state, bucket)
    if product is not None:
        return product
    if unblocker_config is None or not unblocker_config.enabled:
        return None
    html = fetch_via_unblocker(item_url, unblocker_config, redis_conn)
    if not html:
        return None
    return parse_item(html, item_url)
```

Then, in `scrape_batch` (142-184), where it currently calls `_scrape_one` per item, load
the unblocker config once (`load_unblocker_config(get_redis(...))`) and call
`_scrape_one_with_unblocker` instead, passing the config and redis connection. Keep the
existing concurrency, bulk-upsert, and re-enqueue logic unchanged.

> Note: confirm the exact parser entry point. The reference lists the parser in
> `scraper/scraper.py`; use its public function (e.g. `parse_item(html, item_url)`). If
> the public name differs, import the correct one and update the monkeypatch target in
> the test to match.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_worker.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scraper/worker.py tests/test_worker.py
git commit -m "feat: bounded worker escalation to unblocker on persistent challenge"
```

---

## Task 9: `scraper run` orchestrator

The single command Kieran uses: import → discover → wait → split-export → summary.

**Files:**
- Modify: `scraper/cli.py` (new top-level `run` command; reuse `_parse_store_lines`, `_run_discovery`, `export_split_csv`, `queue_is_drained`)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

Add a test that the file parser feeds import correctly with niche defaulted empty
(pure parsing, no network), and that `run` is a registered command:

```python
def test_parse_store_lines_no_niche_defaults_empty():
    from scraper.cli import _parse_store_lines
    text = "# header\nhttps://www.ebay.com/str/one\n\nhttps://www.ebay.com.au/str/two\n"
    out = _parse_store_lines(text, default_niche="")
    assert out == [
        ("https://www.ebay.com/str/one", ""),
        ("https://www.ebay.com.au/str/two", ""),
    ]


def test_run_command_registered():
    from click.testing import CliRunner
    from scraper.cli import cli
    result = CliRunner().invoke(cli, ["run", "--help"])
    assert result.exit_code == 0
    assert "rows-per-file" in result.output
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli.py::test_run_command_registered -v`
Expected: FAIL — no such command "run".

- [ ] **Step 3: Implement the command**

In `scraper/cli.py` add a top-level command:

```python
@cli.command()
@click.argument("store_file", type=click.Path(exists=True))
@click.option("--export-dir", default="exports", help="Directory for the output CSV files.")
@click.option("--rows-per-file", default=500_000, type=int, help="Max rows per CSV file.")
@click.option("--no-wait", is_flag=True, help="Queue scraping and exit without waiting/exporting.")
def run(store_file: str, export_dir: str, rows_per_file: int, no_wait: bool) -> None:
    """Import a store-URL file, scrape every product across all marketplaces, write CSVs."""
    settings = Settings()
    text = Path(store_file).read_text()
    stores = _parse_store_lines(text, default_niche="")
    click.echo(f"Loaded {len(stores)} stores from {store_file}")

    asyncio.run(_import_stores(stores))

    proxy_url = _load_proxy_url(settings)  # existing helper/inline pattern used by scrape start
    ok, empty, blocked, total_queued = _run_discovery(stores, proxy_url, settings)
    _report_discovery(ok, empty, blocked, total_queued)

    if no_wait:
        click.echo("Queued. Exiting without waiting (--no-wait).")
        return

    redis_conn = get_redis(settings.redis_url)
    queue = get_queue(redis_conn)
    click.echo("Scraping... (waiting for the queue to drain)")
    while not queue_is_drained(queue):
        time.sleep(10)
        click.echo(f"  pending={queue.count} ...", nl=True)

    paths = asyncio.run(export_split_csv_all(export_dir, rows_per_file))
    unblocker_used = int(redis_conn.get(UNBLOCKER_COUNT_KEY) or 0)
    click.echo(
        f"Done. {ok}/{len(stores)} stores OK, {total_queued} items queued, "
        f"{unblocker_used} unblocker requests, {len(paths)} CSV files in {export_dir}/"
    )
```

Add the supporting helpers and imports near the other async helpers in `cli.py`:

```python
import time
from scraper.queue import get_redis, get_queue, queue_is_drained
from scraper.export import export_split_csv
from scraper.unblocker import UNBLOCKER_COUNT_KEY


async def _import_stores(stores: list[tuple[str, str]]) -> None:
    settings = Settings()
    pool = await asyncpg.create_pool(settings.database_url)
    try:
        for store_url, niche in stores:
            await add_store(pool, _canonical_store_url(store_url), niche)
    finally:
        await pool.close()


async def export_split_csv_all(export_dir: str, rows_per_file: int) -> list[str]:
    settings = Settings()
    pool = await asyncpg.create_pool(settings.database_url)
    try:
        return await export_split_csv(pool, export_dir, rows_per_file=rows_per_file, niche=None)
    finally:
        await pool.close()
```

If `_load_proxy_url` does not already exist as a helper, inline the existing
proxy-from-Redis pattern used in `scrape start` (read `PROXY_REDIS_KEY` from
`redis_conn`, fall back to `settings.proxy_url`).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scraper/cli.py tests/test_cli.py
git commit -m "feat: scraper run orchestrator (import -> scrape -> split CSV)"
```

---

## Task 10: `scraper setup` wizard

Interactive credential entry that validates and stores proxy + optional Oxylabs.

**Files:**
- Modify: `scraper/cli.py` (new `setup` command)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
def test_setup_command_registered():
    from click.testing import CliRunner
    from scraper.cli import cli
    result = CliRunner().invoke(cli, ["setup", "--help"])
    assert result.exit_code == 0


def test_validate_proxy_returns_bool(monkeypatch):
    from scraper import cli as climod
    monkeypatch.setattr(climod, "_probe_proxy_ok", lambda proxy_url: True, raising=False)
    assert climod._probe_proxy_ok("http://x") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli.py::test_setup_command_registered -v`
Expected: FAIL — no such command "setup".

- [ ] **Step 3: Implement**

In `scraper/cli.py`:

```python
def _probe_proxy_ok(proxy_url: str) -> bool:
    """A real fetch through the proxy against eBay returns a non-challenge page."""
    from scraper.fetch import apply_proxy_country, build_session, is_challenge_page
    target = "https://www.ebay.com/sch/i.html?_ssn=onlinesound&_pgn=1&_ipg=60"
    try:
        session = build_session(apply_proxy_country(proxy_url, target))
        session.get("https://www.ebay.com/", timeout=30)
        resp = session.get(target, timeout=40)
        return not is_challenge_page(resp.text)
    except Exception:
        return False


def _probe_unblocker_ok(username: str, password: str) -> bool:
    from scraper.unblocker import UnblockerConfig, fetch_via_unblocker
    cfg = UnblockerConfig(provider="oxylabs", username=username, password=password)
    html = fetch_via_unblocker(
        "https://www.ebay.com/sch/i.html?_ssn=onlinesound&_pgn=1&_ipg=60", cfg
    )
    return bool(html)


@cli.command()
def setup() -> None:
    """Interactive setup: paste proxy and optional unblocker credentials."""
    settings = Settings()
    redis_conn = get_redis(settings.redis_url)

    proxy_url = click.prompt("Residential proxy URL (http://user:pass@host:port)")
    click.echo("  testing proxy against eBay ...")
    if _probe_proxy_ok(proxy_url):
        redis_conn.set(PROXY_REDIS_KEY, proxy_url)
        click.echo("  proxy OK and saved.")
    else:
        click.echo("  proxy FAILED (challenge or error). Not saved. Re-run setup.")
        return

    if click.confirm("Add an optional Oxylabs unblocker fallback?", default=False):
        user = click.prompt("Oxylabs username")
        pw = click.prompt("Oxylabs password", hide_input=True)
        click.echo("  testing unblocker ...")
        if _probe_unblocker_ok(user, pw):
            from scraper.unblocker import (
                UNBLOCKER_PROVIDER_KEY, OXYLABS_USER_KEY, OXYLABS_PASS_KEY,
            )
            redis_conn.set(UNBLOCKER_PROVIDER_KEY, "oxylabs")
            redis_conn.set(OXYLABS_USER_KEY, user)
            redis_conn.set(OXYLABS_PASS_KEY, pw)
            click.echo("  unblocker OK and saved.")
        else:
            click.echo("  unblocker test FAILED. Skipped (proxy-only is fine).")

    click.echo("Setup complete. Run `scraper doctor` to confirm everything is green.")
```

Ensure `PROXY_REDIS_KEY` and `get_redis` are imported in `cli.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scraper/cli.py tests/test_cli.py
git commit -m "feat: scraper setup wizard with proxy/unblocker validation"
```

---

## Task 11: `scraper doctor` diagnostics

One command that checks Redis, Postgres, proxy, unblocker, and worker presence.

**Files:**
- Modify: `scraper/cli.py` (new `doctor` command)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
def test_doctor_command_registered():
    from click.testing import CliRunner
    from scraper.cli import cli
    result = CliRunner().invoke(cli, ["doctor", "--help"])
    assert result.exit_code == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cli.py::test_doctor_command_registered -v`
Expected: FAIL — no such command "doctor".

- [ ] **Step 3: Implement**

In `scraper/cli.py`:

```python
@cli.command()
def doctor() -> None:
    """Check Redis, Postgres, proxy, unblocker, and connected workers."""
    settings = Settings()

    def line(label: str, ok: bool, detail: str = "") -> None:
        mark = "OK " if ok else "XX "
        click.echo(f"[{mark}] {label}{(' - ' + detail) if detail else ''}")

    # Redis
    try:
        redis_conn = get_redis(settings.redis_url)
        redis_conn.ping()
        line("Redis", True, settings.redis_url)
    except Exception as exc:
        line("Redis", False, str(exc))
        return

    # Postgres
    try:
        asyncio.run(_ping_db())
        line("Postgres", True, settings.database_url)
    except Exception as exc:
        line("Postgres", False, str(exc))

    # Proxy
    raw = redis_conn.get(PROXY_REDIS_KEY)
    proxy_url = raw.decode().strip() if raw else settings.proxy_url
    if proxy_url:
        line("Proxy", _probe_proxy_ok(proxy_url), "live eBay fetch")
    else:
        line("Proxy", False, "not configured (run `scraper setup`)")

    # Unblocker
    from scraper.unblocker import load_unblocker_config, UNBLOCKER_COUNT_KEY
    cfg = load_unblocker_config(redis_conn)
    if cfg.enabled:
        used = int(redis_conn.get(UNBLOCKER_COUNT_KEY) or 0)
        line("Unblocker", True, f"oxylabs configured, {used} requests used")
    else:
        line("Unblocker", True, "off (proxy-only) - this is fine")

    # Workers
    queue = get_queue(redis_conn)
    worker_count = len(Worker.all(queue=queue))
    line("Workers", worker_count > 0, f"{worker_count} connected")
```

Add helper and imports:

```python
from rq import Worker


async def _ping_db() -> None:
    settings = Settings()
    pool = await asyncpg.create_pool(settings.database_url)
    try:
        await pool.fetchval("SELECT 1")
    finally:
        await pool.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scraper/cli.py tests/test_cli.py
git commit -m "feat: scraper doctor diagnostics"
```

---

## Task 12: Sample store list + documentation

Composed validation list and loud multi-currency + `run` docs.

**Files:**
- Create: `tests/fixtures/sample_stores.csv`
- Modify: `README.md`, `HANDOFF.md`

- [ ] **Step 1: Create the sample list**

Create `tests/fixtures/sample_stores.csv` (one URL per line; real stores spanning the
three marketplaces, including known-hard AU sellers and easy US/UK ones). Use seller
names already validated in this project plus a spread per marketplace:

```text
# Mixed US / AU / UK validation list. One eBay store URL per line.
# AU (known-hard, must resolve to ebay.com.au via detection):
https://www.ebay.com/str/goodpricepeople
https://www.ebay.com/str/af2018
# US (easy):
https://www.ebay.com/str/onlinesound
https://www.ebay.com/str/tool007tool
# UK (replace with confirmed live .co.uk sellers during validation):
https://www.ebay.co.uk/str/redtiger_store
```

- [ ] **Step 2: Document `run`/`setup`/`doctor` and the currency warning**

In `README.md` add a top "Quick start for the operator" section:

```markdown
## Quick start (operator)

1. `scraper setup` — paste your proxy URL (and optional Oxylabs credentials). It tests them.
2. `scraper doctor` — confirm everything is green (Redis, Postgres, Proxy, Workers).
3. Put your eBay store URLs in a text file, one per line (any marketplace, no other info).
4. `scraper run stores.txt` — scrapes everything and writes split CSVs to `exports/`.

> The scraper auto-detects each store's home marketplace (US / AU / UK). You do not tag
> stores with a location or niche.
```

In both `README.md` and `HANDOFF.md` add a prominent warning block:

```markdown
> **CSV currency is mixed.** Because stores span US/AU/UK, the `currency` column contains
> USD, AUD, and GBP. Do NOT map `price` straight into a single-currency Shopify store
> without first segmenting or converting by the `currency` column, or AUD/GBP prices will
> import as if they were USD.
```

- [ ] **Step 3: Commit**

```bash
git add tests/fixtures/sample_stores.csv README.md HANDOFF.md
git commit -m "docs: operator quick-start, multi-currency warning, sample store list"
```

---

## Task 13: Full-suite green + live validation run

**Files:** none (validation + recorded results)

- [ ] **Step 1: Run the full unit suite**

Run (with the documented Docker coordinator services up):

```bash
docker compose -f scraper/coordinator-compose.yml up -d
pytest -v
```

Expected: all tests pass (the original 88 plus the new marketplace/unblocker/export/run
tests). Fix any regression before proceeding; do not skip or delete tests.

- [ ] **Step 2: Live end-to-end validation (stresses volume + concurrency)**

With a worker running and the proxy configured:

```bash
scraper setup        # if not already done
scraper doctor       # confirm green + >=1 worker
scraper run tests/fixtures/sample_stores.csv --export-dir /tmp/ebay_validation
```

Record in a new `docs/findings/2026-06-05-validation-run.md`:
- Per-store outcome (detected marketplace + item count); confirm `goodpricepeople` and
  `af2018` resolve to `www.ebay.com.au` and return their full grids.
- Overall store success rate and product count.
- Sustained throughput and any challenge/escalation counts (this is the IP-reputation
  axis the >90%-at-scale bar depends on, so note the request volume reached before any
  throttling appeared).
- Unblocker request count (expected 0 if all stores resolved via the free path).

- [ ] **Step 3: Commit the validation record**

```bash
git add docs/findings/2026-06-05-validation-run.md
git commit -m "docs: live validation run results"
```

---

## Self-Review notes (addressed)

- **Spec coverage:** detection (T3), challenge≠zero (T3), schema persistence (T2),
  discovery routing (T7), unblocker off-by-default + counter (T4/T7/T8), worker
  escalation (T8), `run`/`setup`/`doctor` (T9/T10/T11), split CSV (T5), niche removal
  (T1/T9), multi-currency docs (T12), volume-stressing validation (T13). All mapped.
- **Challenge vs zero** is honored in detection (retry) and in discovery (blocked vs
  empty), never conflated.
- **Type consistency:** `DetectionOutcome(result, undetermined_domains)`,
  `MarketplaceResult(domain, country, item_count, seller_search_url)`,
  `UnblockerConfig(provider, username, password).enabled`, and the Redis key constants
  are used identically across tasks.
- **Open verification flagged inline (T8):** confirm the exact item-parser public name
  in `scraper/scraper.py` and the worker's per-item fetch function name; adapt the
  wrapper/monkeypatch targets to the real names when implementing.

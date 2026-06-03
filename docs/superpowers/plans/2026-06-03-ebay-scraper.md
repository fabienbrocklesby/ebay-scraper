# eBay Scraper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a distributed eBay store scraper that crawls seller stores by URL, extracts full product data, stores results in Postgres, and exports to CSV via a CLI.

**Architecture:** A coordinator VPS runs Redis (rq job queue) and Postgres (results store). The CLI pushes item IDs into the queue when stores are added. Worker VPS nodes run rq workers that pull item IDs, scrape eBay detail pages with httpx, and write to Postgres via psycopg2. The CLI handles export, status, and schema init.

**Tech Stack:** Python 3.11+, httpx, beautifulsoup4, rq, psycopg2-binary, asyncpg, click, python-dotenv, pytest, respx

---

## File Map

```
ebay-scraper/
├── scraper/
│   ├── __init__.py
│   ├── cli.py          - Click CLI: add, worker start, export, status, db init, clear
│   ├── config.py       - Settings loaded from .env
│   ├── db.py           - Postgres schema + async CRUD (asyncpg, used by CLI)
│   ├── queue.py        - rq job queue helpers: enqueue with dedup, get queue/redis
│   ├── store.py        - eBay store paginator: given store URL, returns list of item IDs
│   ├── scraper.py      - eBay item detail page parser: returns ProductData dataclass (includes mpn, upc)
│   ├── worker.py       - rq job function scrape_and_store (sync, psycopg2), start_worker
│   └── export.py       - Queries Postgres, writes CSV
├── tests/
│   ├── conftest.py     - Shared pytest fixtures
│   ├── test_config.py
│   ├── test_db.py
│   ├── test_queue.py
│   ├── test_store.py
│   ├── test_scraper.py
│   ├── test_worker.py
│   ├── test_export.py
│   └── test_integration.py
├── docker-compose.yml
├── .env.example
├── pyproject.toml
├── requirements.txt
└── README.md
```

**Sync vs async boundary:**
- `scraper.py`, `store.py`, `worker.py`: sync (httpx + psycopg2, safe in rq workers)
- `db.py`, `export.py`: async (asyncpg, called from CLI via `asyncio.run()`)
- `cli.py`: sync Click commands, wraps async calls in `asyncio.run()`

---

## Task 1: Project Scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `requirements.txt`
- Create: `.env.example`
- Create: `docker-compose.yml`
- Create: `scraper/__init__.py`

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.backends.legacy:build"

[project]
name = "ebay-scraper"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "httpx>=0.27",
    "beautifulsoup4>=4.12",
    "rq>=1.16",
    "asyncpg>=0.29",
    "psycopg2-binary>=2.9",
    "click>=8.1",
    "python-dotenv>=1.0",
]

[project.scripts]
scraper = "scraper.cli:cli"

[tool.setuptools.packages.find]
where = ["."]
include = ["scraper*"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

- [ ] **Step 2: Create `requirements.txt`**

```
httpx>=0.27
beautifulsoup4>=4.12
rq>=1.16
asyncpg>=0.29
psycopg2-binary>=2.9
click>=8.1
python-dotenv>=1.0
pytest>=8.0
pytest-asyncio>=0.23
respx>=0.21
```

- [ ] **Step 3: Create `.env.example`**

```
REDIS_URL=redis://localhost:6379
DATABASE_URL=postgresql://scraper:scraper@localhost:5432/ebayscraper
PROXY_URL=
REQUESTS_PER_SECOND=0.5
```

- [ ] **Step 4: Create `docker-compose.yml`**

```yaml
services:
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
    restart: unless-stopped

  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: scraper
      POSTGRES_PASSWORD: scraper
      POSTGRES_DB: ebayscraper
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data
    restart: unless-stopped

volumes:
  postgres_data:
```

- [ ] **Step 5: Create `scraper/__init__.py`** (empty file)

- [ ] **Step 6: Install and verify**

```bash
pip install -r requirements.txt && pip install -e .
```

Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml requirements.txt .env.example docker-compose.yml scraper/__init__.py
git commit -m "chore: project scaffolding"
```

---

## Task 2: Config

**Files:**
- Create: `scraper/config.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_config.py
import pytest
from scraper.config import Settings

def test_loads_from_env(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://test:6379")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@test/db")
    monkeypatch.setenv("PROXY_URL", "")
    monkeypatch.setenv("REQUESTS_PER_SECOND", "1.0")
    s = Settings()
    assert s.redis_url == "redis://test:6379"
    assert s.database_url == "postgresql://u:p@test/db"
    assert s.proxy_url is None
    assert s.requests_per_second == 1.0

def test_proxy_url_none_when_empty(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://test:6379")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@test/db")
    monkeypatch.setenv("PROXY_URL", "")
    s = Settings()
    assert s.proxy_url is None

def test_missing_redis_url_raises(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@test/db")
    with pytest.raises(ValueError, match="REDIS_URL"):
        Settings()

def test_missing_database_url_raises(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://test:6379")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="DATABASE_URL"):
        Settings()

def test_requests_per_second_defaults_to_half(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://test:6379")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@test/db")
    monkeypatch.delenv("REQUESTS_PER_SECOND", raising=False)
    s = Settings()
    assert s.requests_per_second == 0.5
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_config.py -v
```

Expected: `ModuleNotFoundError: No module named 'scraper.config'`

- [ ] **Step 3: Implement `scraper/config.py`**

```python
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass
class Settings:
    redis_url: str
    database_url: str
    proxy_url: str | None
    requests_per_second: float

    def __init__(self):
        redis_url = os.getenv("REDIS_URL")
        if not redis_url:
            raise ValueError("REDIS_URL is required")
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise ValueError("DATABASE_URL is required")
        proxy_raw = os.getenv("PROXY_URL", "")
        self.redis_url = redis_url
        self.database_url = database_url
        self.proxy_url = proxy_raw if proxy_raw else None
        self.requests_per_second = float(os.getenv("REQUESTS_PER_SECOND", "0.5"))
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_config.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add scraper/config.py tests/test_config.py
git commit -m "feat: config module"
```

---

## Task 3: Database (async, used by CLI)

**Files:**
- Create: `scraper/db.py`
- Create: `tests/conftest.py`
- Create: `tests/test_db.py`

The DB module uses asyncpg and is called from CLI commands via `asyncio.run()`. The `ProductRecord` dataclass is the shared data model used by both `db.py` and `worker.py`.

- [ ] **Step 1: Start coordinator services**

```bash
docker compose up -d
docker exec $(docker ps -qf "ancestor=postgres:16-alpine") psql -U scraper -c "CREATE DATABASE ebayscraper_test;" 2>/dev/null || true
```

Expected: services start, test DB created (or already exists).

- [ ] **Step 2: Create `tests/conftest.py`**

```python
# tests/conftest.py
import asyncio
import asyncpg
import pytest

TEST_DB_URL = "postgresql://scraper:scraper@localhost:5432/ebayscraper_test"

@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()

@pytest.fixture(scope="session")
async def db_pool():
    from scraper.db import init_schema
    pool = await asyncpg.create_pool(TEST_DB_URL)
    await init_schema(pool)
    yield pool
    await pool.close()

@pytest.fixture(autouse=True)
async def clean_products(db_pool):
    yield
    await db_pool.execute("DELETE FROM products")
```

- [ ] **Step 3: Write failing tests**

```python
# tests/test_db.py
import pytest
from scraper.db import insert_product, get_products_by_niche, get_counts, clear_niche, ProductRecord

def make_record(**kwargs) -> ProductRecord:
    defaults = dict(
        item_id="123",
        title="Test Product",
        price=9.99,
        currency="USD",
        condition="New",
        description="<p>desc</p>",
        image_urls="https://img.ebay.com/1.jpg|https://img.ebay.com/2.jpg",
        item_url="https://www.ebay.com/itm/123",
        seller_id="testseller",
        store_url="https://www.ebay.com/str/teststore",
        category="Electronics",
        item_specifics='{"brand": "Test"}',
        shipping="Free",
        listing_type="Buy It Now",
        niche="test-niche",
    )
    defaults.update(kwargs)
    return ProductRecord(**defaults)

@pytest.mark.asyncio
async def test_insert_and_retrieve(db_pool):
    await insert_product(db_pool, make_record(item_id="r1", niche="retrieval"))
    results = await get_products_by_niche(db_pool, "retrieval")
    assert len(results) == 1
    assert results[0]["item_id"] == "r1"
    assert results[0]["title"] == "Test Product"

@pytest.mark.asyncio
async def test_duplicate_item_id_upserts(db_pool):
    await insert_product(db_pool, make_record(item_id="dupe1", title="Original", niche="dupe"))
    await insert_product(db_pool, make_record(item_id="dupe1", title="Updated", niche="dupe"))
    results = await get_products_by_niche(db_pool, "dupe")
    assert len(results) == 1
    assert results[0]["title"] == "Updated"

@pytest.mark.asyncio
async def test_clear_niche(db_pool):
    await insert_product(db_pool, make_record(item_id="clr1", niche="to-clear"))
    await clear_niche(db_pool, "to-clear")
    results = await get_products_by_niche(db_pool, "to-clear")
    assert results == []

@pytest.mark.asyncio
async def test_get_products_no_niche_returns_all(db_pool):
    await insert_product(db_pool, make_record(item_id="all1", niche="n1"))
    await insert_product(db_pool, make_record(item_id="all2", niche="n2"))
    results = await get_products_by_niche(db_pool, niche=None)
    assert len(results) == 2

@pytest.mark.asyncio
async def test_get_counts_groups_by_niche(db_pool):
    await insert_product(db_pool, make_record(item_id="cnt1", niche="counted"))
    await insert_product(db_pool, make_record(item_id="cnt2", niche="counted"))
    counts = await get_counts(db_pool)
    assert counts["counted"] == 2
```

- [ ] **Step 4: Run tests to verify they fail**

```bash
pytest tests/test_db.py -v
```

Expected: `ModuleNotFoundError: No module named 'scraper.db'`

- [ ] **Step 5: Implement `scraper/db.py`**

```python
import asyncpg
from dataclasses import dataclass
from datetime import datetime, timezone

@dataclass
class ProductRecord:
    item_id: str
    title: str
    price: float
    currency: str
    condition: str
    description: str
    image_urls: str
    item_url: str
    seller_id: str
    store_url: str
    category: str
    item_specifics: str
    mpn: str
    upc: str
    shipping: str
    listing_type: str
    niche: str

async def init_schema(pool: asyncpg.Pool) -> None:
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS products (
            item_id      TEXT PRIMARY KEY,
            title        TEXT,
            price        NUMERIC,
            currency     TEXT,
            condition    TEXT,
            description  TEXT,
            image_urls   TEXT,
            item_url     TEXT,
            seller_id    TEXT,
            store_url    TEXT,
            category     TEXT,
            item_specifics TEXT,
            mpn          TEXT,
            upc          TEXT,
            shipping     TEXT,
            listing_type TEXT,
            niche        TEXT,
            scraped_at   TIMESTAMPTZ DEFAULT now()
        )
    """)

async def insert_product(pool: asyncpg.Pool, record: ProductRecord) -> None:
    await pool.execute("""
        INSERT INTO products (
            item_id, title, price, currency, condition, description,
            image_urls, item_url, seller_id, store_url, category,
            item_specifics, mpn, upc, shipping, listing_type, niche, scraped_at
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
        ON CONFLICT (item_id) DO UPDATE SET
            title=EXCLUDED.title, price=EXCLUDED.price, currency=EXCLUDED.currency,
            condition=EXCLUDED.condition, description=EXCLUDED.description,
            image_urls=EXCLUDED.image_urls, item_url=EXCLUDED.item_url,
            seller_id=EXCLUDED.seller_id, store_url=EXCLUDED.store_url,
            category=EXCLUDED.category, item_specifics=EXCLUDED.item_specifics,
            mpn=EXCLUDED.mpn, upc=EXCLUDED.upc,
            shipping=EXCLUDED.shipping, listing_type=EXCLUDED.listing_type,
            niche=EXCLUDED.niche, scraped_at=EXCLUDED.scraped_at
    """,
        record.item_id, record.title, record.price, record.currency,
        record.condition, record.description, record.image_urls, record.item_url,
        record.seller_id, record.store_url, record.category, record.item_specifics,
        record.mpn, record.upc, record.shipping, record.listing_type, record.niche,
        datetime.now(timezone.utc),
    )

async def get_products_by_niche(
    pool: asyncpg.Pool, niche: str | None
) -> list[asyncpg.Record]:
    if niche:
        return await pool.fetch("SELECT * FROM products WHERE niche = $1", niche)
    return await pool.fetch("SELECT * FROM products")

async def get_counts(pool: asyncpg.Pool) -> dict[str, int]:
    rows = await pool.fetch(
        "SELECT niche, COUNT(*) AS count FROM products GROUP BY niche"
    )
    return {row["niche"]: int(row["count"]) for row in rows}

async def clear_niche(pool: asyncpg.Pool, niche: str) -> None:
    await pool.execute("DELETE FROM products WHERE niche = $1", niche)
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
pytest tests/test_db.py -v
```

Expected: 5 passed.

- [ ] **Step 7: Commit**

```bash
git add scraper/db.py tests/conftest.py tests/test_db.py
git commit -m "feat: database module with schema and async CRUD"
```

---

## Task 4: Store Crawler

**Files:**
- Create: `scraper/store.py`
- Create: `tests/test_store.py`

Paginates through an eBay seller store by hitting the seller search URL, extracting item IDs from listing cards on each page.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_store.py
import pytest
import respx
import httpx
from scraper.store import extract_seller_id, build_store_search_url, get_item_ids_from_store

def test_extract_seller_id_from_str_url():
    assert extract_seller_id("https://www.ebay.com/str/coolstore") == "coolstore"

def test_extract_seller_id_from_str_url_trailing_slash():
    assert extract_seller_id("https://www.ebay.com/str/coolstore/") == "coolstore"

def test_extract_seller_id_from_sch_url():
    assert extract_seller_id("https://www.ebay.com/sch/username/m.html") == "username"

def test_build_store_search_url_contains_seller_and_page():
    url = build_store_search_url("myseller", page=3)
    assert "myseller" in url
    assert "_pgn=3" in url
    assert "_ipg=240" in url

@respx.mock
def test_get_item_ids_from_store_single_page():
    html = """
    <html><body>
    <ul class="srp-results">
      <li class="s-item">
        <a class="s-item__link" href="https://www.ebay.com/itm/111111111">Item 1</a>
      </li>
      <li class="s-item">
        <a class="s-item__link" href="https://www.ebay.com/itm/222222222">Item 2</a>
      </li>
    </ul>
    </body></html>
    """
    respx.get("https://www.ebay.com/sch/i.html").mock(
        return_value=httpx.Response(200, text=html)
    )
    ids = get_item_ids_from_store("https://www.ebay.com/str/testseller", max_pages=1)
    assert "111111111" in ids
    assert "222222222" in ids
    assert len(ids) == 2

@respx.mock
def test_get_item_ids_returns_empty_on_no_listings():
    html = "<html><body><ul class='srp-results'></ul></body></html>"
    respx.get("https://www.ebay.com/sch/i.html").mock(
        return_value=httpx.Response(200, text=html)
    )
    ids = get_item_ids_from_store("https://www.ebay.com/str/emptyseller", max_pages=1)
    assert ids == []

@respx.mock
def test_get_item_ids_deduplicates():
    html = """
    <html><body>
    <ul class="srp-results">
      <li class="s-item">
        <a class="s-item__link" href="https://www.ebay.com/itm/111">Item 1</a>
      </li>
      <li class="s-item">
        <a class="s-item__link" href="https://www.ebay.com/itm/111">Item 1 again</a>
      </li>
    </ul>
    </body></html>
    """
    respx.get("https://www.ebay.com/sch/i.html").mock(
        return_value=httpx.Response(200, text=html)
    )
    ids = get_item_ids_from_store("https://www.ebay.com/str/dupe", max_pages=1)
    assert ids.count("111") == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_store.py -v
```

Expected: `ModuleNotFoundError: No module named 'scraper.store'`

- [ ] **Step 3: Implement `scraper/store.py`**

```python
import re
import time
import httpx
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urlencode

EBAY_SEARCH_BASE = "https://www.ebay.com/sch/i.html"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

def extract_seller_id(store_url: str) -> str:
    path = urlparse(store_url).path.strip("/")
    parts = path.split("/")
    if "str" in parts:
        return parts[parts.index("str") + 1]
    if "sch" in parts:
        return parts[parts.index("sch") + 1]
    raise ValueError(f"Cannot extract seller ID from: {store_url}")

def build_store_search_url(seller_id: str, page: int) -> str:
    params = {"_ssn": seller_id, "_ipg": "240", "_pgn": str(page)}
    return f"{EBAY_SEARCH_BASE}?{urlencode(params)}"

def _extract_item_ids(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    ids = []
    for a in soup.select("a.s-item__link"):
        href = a.get("href", "")
        match = re.search(r"/itm/(\d+)", href)
        if match:
            ids.append(match.group(1))
    return ids

def _has_next_page(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    return bool(soup.select_one("a.pagination__next"))

def get_item_ids_from_store(
    store_url: str,
    proxy_url: str | None = None,
    requests_per_second: float = 0.5,
    max_pages: int = 9999,
) -> list[str]:
    seller_id = extract_seller_id(store_url)
    proxies = {"http://": proxy_url, "https://": proxy_url} if proxy_url else None
    delay = 1.0 / requests_per_second if requests_per_second > 0 else 2.0
    seen: dict[str, None] = {}

    with httpx.Client(headers=HEADERS, proxies=proxies, timeout=30) as client:
        for page in range(1, max_pages + 1):
            url = build_store_search_url(seller_id, page)
            response = client.get(url)
            response.raise_for_status()
            ids = _extract_item_ids(response.text)
            for item_id in ids:
                seen[item_id] = None
            if not ids or not _has_next_page(response.text):
                break
            time.sleep(delay)

    return list(seen.keys())
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_store.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add scraper/store.py tests/test_store.py
git commit -m "feat: eBay store crawler with pagination and deduplication"
```

---

## Task 5: Item Scraper

**Files:**
- Create: `scraper/scraper.py`
- Create: `tests/test_scraper.py`

Fetches a single eBay item detail page, extracts product data from JSON-LD schema markup, returns a `ProductData` dataclass or `None` if the item is unavailable.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_scraper.py
import pytest
import respx
import httpx
from scraper.scraper import scrape_item, ProductData

SAMPLE_HTML = """
<html>
<head>
<script type="application/ld+json">
{
  "@type": "Product",
  "name": "Genuine OEM Toyota Brake Pads",
  "description": "<p>Brand new genuine brake pads</p>",
  "image": [
    "https://i.ebayimg.com/images/g/abc/s-l1600.jpg",
    "https://i.ebayimg.com/images/g/def/s-l1600.jpg"
  ],
  "offers": {
    "@type": "Offer",
    "price": "45.99",
    "priceCurrency": "USD",
    "itemCondition": "https://schema.org/NewCondition",
    "seller": {"@type": "Person", "name": "autopartsking"}
  },
  "category": "Auto Parts"
}
</script>
</head>
<body>
<div class="ux-layout-section--features">
  <dl>
    <dt>Brand</dt><dd>Toyota</dd>
    <dt>Part Number</dt><dd>04465-33130</dd>
  </dl>
</div>
<span class="ux-textspans--BOLD">Free shipping</span>
<span class="ux-textspans">Buy It Now</span>
</body>
</html>
"""

@respx.mock
def test_scrape_item_returns_product_data():
    respx.get("https://www.ebay.com/itm/123456789").mock(
        return_value=httpx.Response(200, text=SAMPLE_HTML)
    )
    product = scrape_item("123456789")
    assert isinstance(product, ProductData)
    assert product.item_id == "123456789"
    assert product.title == "Genuine OEM Toyota Brake Pads"
    assert product.price == 45.99
    assert product.currency == "USD"
    assert product.condition == "New"
    assert product.seller_id == "autopartsking"
    assert product.category == "Auto Parts"

@respx.mock
def test_scrape_item_image_urls_pipe_separated():
    respx.get("https://www.ebay.com/itm/999999999").mock(
        return_value=httpx.Response(200, text=SAMPLE_HTML)
    )
    product = scrape_item("999999999")
    urls = product.image_urls.split("|")
    assert len(urls) == 2
    assert all("ebayimg.com" in u for u in urls)

@respx.mock
def test_scrape_item_returns_none_on_404():
    respx.get("https://www.ebay.com/itm/000000000").mock(
        return_value=httpx.Response(404, text="Not Found")
    )
    result = scrape_item("000000000")
    assert result is None

@respx.mock
def test_scrape_item_item_specifics_json():
    respx.get("https://www.ebay.com/itm/123456789").mock(
        return_value=httpx.Response(200, text=SAMPLE_HTML)
    )
    import json
    product = scrape_item("123456789")
    specifics = json.loads(product.item_specifics)
    assert specifics.get("Brand") == "Toyota"
    assert specifics.get("Part Number") == "04465-33130"

@respx.mock
def test_scrape_item_shipping_and_listing_type():
    respx.get("https://www.ebay.com/itm/123456789").mock(
        return_value=httpx.Response(200, text=SAMPLE_HTML)
    )
    product = scrape_item("123456789")
    assert "free" in product.shipping.lower()
    assert product.listing_type == "Buy It Now"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_scraper.py -v
```

Expected: `ModuleNotFoundError: No module named 'scraper.scraper'`

- [ ] **Step 3: Implement `scraper/scraper.py`**

```python
import json
from dataclasses import dataclass
from typing import Optional
import httpx
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

@dataclass
class ProductData:
    item_id: str
    title: str
    price: float
    currency: str
    condition: str
    description: str
    image_urls: str
    item_url: str
    seller_id: str
    category: str
    item_specifics: str
    mpn: str
    upc: str
    shipping: str
    listing_type: str

def _condition_from_schema(schema_url: str) -> str:
    mapping = {
        "NewCondition": "New",
        "UsedCondition": "Used",
        "RefurbishedCondition": "Refurbished",
        "ForParts": "For Parts",
    }
    for key, val in mapping.items():
        if key in schema_url:
            return val
    return schema_url.split("/")[-1] if schema_url else ""

def _extract_item_specifics(soup: BeautifulSoup) -> str:
    specifics: dict[str, str] = {}
    section = soup.select_one("div.ux-layout-section--features")
    if section:
        for dt, dd in zip(section.select("dt"), section.select("dd")):
            key = dt.get_text(strip=True)
            val = dd.get_text(strip=True)
            if key and val:
                specifics[key] = val
    return json.dumps(specifics)

def _extract_identifier(specifics_json: str, *keys: str) -> str:
    try:
        specifics = json.loads(specifics_json)
    except (json.JSONDecodeError, TypeError):
        return ""
    for key in keys:
        val = specifics.get(key, "")
        if val:
            return val
    return ""

def _extract_shipping(soup: BeautifulSoup) -> str:
    for span in soup.select("span.ux-textspans--BOLD"):
        text = span.get_text(strip=True)
        if "ship" in text.lower() or "free" in text.lower():
            return text
    return ""

def _extract_listing_type(soup: BeautifulSoup) -> str:
    for span in soup.select("span.ux-textspans"):
        text = span.get_text(strip=True)
        if "Buy It Now" in text:
            return "Buy It Now"
        if "Auction" in text:
            return "Auction"
    return ""

def scrape_item(
    item_id: str,
    proxy_url: Optional[str] = None,
    client: Optional[httpx.Client] = None,
) -> Optional[ProductData]:
    url = f"https://www.ebay.com/itm/{item_id}"
    proxies = {"http://": proxy_url, "https://": proxy_url} if proxy_url else None
    own_client = client is None

    try:
        if own_client:
            client = httpx.Client(headers=HEADERS, proxies=proxies, timeout=30)
        response = client.get(url)
        if response.status_code == 404:
            return None
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    finally:
        if own_client and client:
            client.close()

    soup = BeautifulSoup(response.text, "html.parser")
    ld_json = None
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
            if isinstance(data, dict) and data.get("@type") == "Product":
                ld_json = data
                break
        except (json.JSONDecodeError, AttributeError):
            continue

    if not ld_json:
        return None

    offers = ld_json.get("offers", {})
    images = ld_json.get("image", [])
    if isinstance(images, str):
        images = [images]

    return ProductData(
        item_id=item_id,
        title=ld_json.get("name", ""),
        price=float(offers.get("price", 0)),
        currency=offers.get("priceCurrency", ""),
        condition=_condition_from_schema(offers.get("itemCondition", "")),
        description=str(ld_json.get("description", "")),
        image_urls="|".join(images),
        item_url=url,
        seller_id=(offers.get("seller") or {}).get("name", ""),
        category=ld_json.get("category", ""),
        item_specifics=_extract_item_specifics(soup),
        mpn=_extract_identifier(_extract_item_specifics(soup), "MPN", "Manufacturer Part Number", "mpn"),
        upc=_extract_identifier(_extract_item_specifics(soup), "UPC", "EAN", "ISBN", "upc"),
        shipping=_extract_shipping(soup),
        listing_type=_extract_listing_type(soup),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_scraper.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add scraper/scraper.py tests/test_scraper.py
git commit -m "feat: eBay item detail page scraper"
```

---

## Task 6: Worker (rq job, sync)

**Files:**
- Create: `scraper/worker.py`
- Create: `tests/test_worker.py`

`scrape_and_store` is a sync function enqueued by rq. It uses psycopg2 directly (not asyncpg) to avoid asyncio event loop complications inside rq worker processes. `start_worker` starts the rq worker loop.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_worker.py
import pytest
from unittest.mock import MagicMock, patch
from scraper.scraper import ProductData

def make_mock_product():
    return ProductData(
        item_id="123",
        title="Test Product",
        price=9.99,
        currency="USD",
        condition="New",
        description="",
        image_urls="https://img.ebay.com/1.jpg",
        item_url="https://www.ebay.com/itm/123",
        seller_id="seller1",
        category="Electronics",
        item_specifics="{}",
        shipping="Free",
        listing_type="Buy It Now",
    )

def test_scrape_and_store_inserts_on_success(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("DATABASE_URL", "postgresql://scraper:scraper@localhost/ebayscraper")
    monkeypatch.setenv("PROXY_URL", "")

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.__enter__ = lambda s: s
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_cursor.__enter__ = lambda s: s
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value = mock_cursor

    with patch("scraper.worker.scrape_item", return_value=make_mock_product()), \
         patch("scraper.worker.psycopg2.connect", return_value=mock_conn):
        from scraper.worker import scrape_and_store
        scrape_and_store("123", "electronics", "https://www.ebay.com/str/test")
        assert mock_cursor.execute.called
        assert mock_conn.commit.called

def test_scrape_and_store_skips_on_none_result(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("DATABASE_URL", "postgresql://scraper:scraper@localhost/ebayscraper")
    monkeypatch.setenv("PROXY_URL", "")

    mock_conn = MagicMock()

    with patch("scraper.worker.scrape_item", return_value=None), \
         patch("scraper.worker.psycopg2.connect", return_value=mock_conn):
        from scraper.worker import scrape_and_store
        scrape_and_store("000", "electronics", "https://www.ebay.com/str/test")
        assert not mock_conn.commit.called
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_worker.py -v
```

Expected: `ModuleNotFoundError: No module named 'scraper.worker'`

- [ ] **Step 3: Implement `scraper/worker.py`**

```python
import psycopg2
from datetime import datetime, timezone
from typing import Optional
from scraper.config import Settings
from scraper.scraper import scrape_item, ProductData

INSERT_SQL = """
    INSERT INTO products (
        item_id, title, price, currency, condition, description,
        image_urls, item_url, seller_id, store_url, category,
        item_specifics, mpn, upc, shipping, listing_type, niche, scraped_at
    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    ON CONFLICT (item_id) DO UPDATE SET
        title=EXCLUDED.title, price=EXCLUDED.price, currency=EXCLUDED.currency,
        condition=EXCLUDED.condition, description=EXCLUDED.description,
        image_urls=EXCLUDED.image_urls, item_url=EXCLUDED.item_url,
        seller_id=EXCLUDED.seller_id, store_url=EXCLUDED.store_url,
        category=EXCLUDED.category, item_specifics=EXCLUDED.item_specifics,
        mpn=EXCLUDED.mpn, upc=EXCLUDED.upc,
        shipping=EXCLUDED.shipping, listing_type=EXCLUDED.listing_type,
        niche=EXCLUDED.niche, scraped_at=EXCLUDED.scraped_at
"""

def scrape_and_store(item_id: str, niche: str, store_url: str) -> None:
    settings = Settings()
    product: Optional[ProductData] = scrape_item(item_id, proxy_url=settings.proxy_url)
    if product is None:
        return

    conn = psycopg2.connect(settings.database_url)
    try:
        with conn.cursor() as cur:
            cur.execute(INSERT_SQL, (
                product.item_id, product.title, product.price, product.currency,
                product.condition, product.description, product.image_urls,
                product.item_url, product.seller_id, store_url, product.category,
                product.item_specifics, product.mpn, product.upc,
                product.shipping, product.listing_type,
                niche, datetime.now(timezone.utc),
            ))
        conn.commit()
    finally:
        conn.close()

def start_worker(redis_url: str) -> None:
    import redis as redis_lib
    from rq import Worker
    conn = redis_lib.from_url(redis_url)
    worker = Worker(["scraping"], connection=conn)
    worker.work(with_scheduler=True)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_worker.py -v
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add scraper/worker.py tests/test_worker.py
git commit -m "feat: rq worker job and start_worker"
```

---

## Task 7: Queue Module

**Files:**
- Create: `scraper/queue.py`
- Create: `tests/test_queue.py`

Wraps rq and Redis. Uses a Redis set (`scraped_items`) to track which item IDs have already been queued, preventing duplicate jobs.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_queue.py
from unittest.mock import MagicMock
from scraper.queue import enqueue_items, is_item_queued, mark_item_queued

def make_mock_redis(already_queued: set = None):
    r = MagicMock()
    already_queued = already_queued or set()
    r.sismember = MagicMock(side_effect=lambda key, val: val in already_queued)
    r.sadd = MagicMock()
    return r

def test_enqueue_items_enqueues_all_new():
    mock_redis = make_mock_redis()
    mock_queue = MagicMock()
    count = enqueue_items(
        mock_queue, mock_redis, ["111", "222", "333"],
        niche="test", store_url="https://www.ebay.com/str/s"
    )
    assert mock_queue.enqueue.call_count == 3
    assert count == 3

def test_enqueue_items_skips_already_queued():
    mock_redis = make_mock_redis(already_queued={"111"})
    mock_queue = MagicMock()
    count = enqueue_items(
        mock_queue, mock_redis, ["111", "222"],
        niche="test", store_url="https://www.ebay.com/str/s"
    )
    assert mock_queue.enqueue.call_count == 1
    assert count == 1

def test_is_item_queued_true_when_member():
    r = make_mock_redis(already_queued={"abc"})
    assert is_item_queued(r, "abc") is True

def test_is_item_queued_false_when_not_member():
    r = make_mock_redis()
    assert is_item_queued(r, "xyz") is False

def test_mark_item_queued_calls_sadd():
    r = make_mock_redis()
    mark_item_queued(r, "999")
    r.sadd.assert_called_once_with("scraped_items", "999")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_queue.py -v
```

Expected: `ModuleNotFoundError: No module named 'scraper.queue'`

- [ ] **Step 3: Implement `scraper/queue.py`**

```python
import redis as redis_lib
from rq import Queue
from scraper.worker import scrape_and_store

SCRAPED_SET_KEY = "scraped_items"

def get_redis(redis_url: str) -> redis_lib.Redis:
    return redis_lib.from_url(redis_url)

def get_queue(redis_conn: redis_lib.Redis) -> Queue:
    return Queue("scraping", connection=redis_conn)

def is_item_queued(redis_conn: redis_lib.Redis, item_id: str) -> bool:
    return bool(redis_conn.sismember(SCRAPED_SET_KEY, item_id))

def mark_item_queued(redis_conn: redis_lib.Redis, item_id: str) -> None:
    redis_conn.sadd(SCRAPED_SET_KEY, item_id)

def enqueue_items(
    queue: Queue,
    redis_conn: redis_lib.Redis,
    item_ids: list[str],
    niche: str,
    store_url: str,
) -> int:
    enqueued = 0
    for item_id in item_ids:
        if is_item_queued(redis_conn, item_id):
            continue
        queue.enqueue(
            scrape_and_store,
            item_id,
            niche,
            store_url,
            job_timeout=300,
        )
        mark_item_queued(redis_conn, item_id)
        enqueued += 1
    return enqueued
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_queue.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add scraper/queue.py tests/test_queue.py
git commit -m "feat: Redis queue module with deduplication"
```

---

## Task 8: Export

**Files:**
- Create: `scraper/export.py`
- Create: `tests/test_export.py`

Queries Postgres via asyncpg and writes a CSV with all product fields.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_export.py
import csv
import pytest
from scraper.export import export_to_csv
from scraper.db import insert_product, ProductRecord

def make_record(**kwargs) -> ProductRecord:
    defaults = dict(
        item_id="e1",
        title="Export Product",
        price=29.99,
        currency="USD",
        condition="New",
        description="<p>desc</p>",
        image_urls="https://img.ebay.com/1.jpg|https://img.ebay.com/2.jpg",
        item_url="https://www.ebay.com/itm/e1",
        seller_id="exportseller",
        store_url="https://www.ebay.com/str/exportstore",
        category="Electronics",
        item_specifics='{"brand": "Test"}',
        shipping="Free",
        listing_type="Buy It Now",
        niche="export-niche",
    )
    defaults.update(kwargs)
    return ProductRecord(**defaults)

@pytest.mark.asyncio
async def test_export_writes_csv(db_pool, tmp_path):
    await insert_product(db_pool, make_record(item_id="ex1", niche="export-niche"))
    output = tmp_path / "out.csv"
    count = await export_to_csv(db_pool, output_path=str(output), niche="export-niche")
    assert count == 1
    assert output.exists()
    with open(output) as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["item_id"] == "ex1"
    assert rows[0]["title"] == "Export Product"
    assert rows[0]["image_urls"] == "https://img.ebay.com/1.jpg|https://img.ebay.com/2.jpg"

@pytest.mark.asyncio
async def test_export_no_niche_exports_all(db_pool, tmp_path):
    await insert_product(db_pool, make_record(item_id="ex2", niche="n1"))
    await insert_product(db_pool, make_record(item_id="ex3", niche="n2"))
    output = tmp_path / "all.csv"
    count = await export_to_csv(db_pool, output_path=str(output), niche=None)
    assert count == 2
    with open(output) as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2

@pytest.mark.asyncio
async def test_export_csv_has_all_columns(db_pool, tmp_path):
    await insert_product(db_pool, make_record(item_id="ex4", niche="cols"))
    output = tmp_path / "cols.csv"
    await export_to_csv(db_pool, output_path=str(output), niche="cols")
    with open(output) as f:
        reader = csv.DictReader(f)
        columns = reader.fieldnames
    expected = [
        "item_id", "title", "price", "currency", "condition", "description",
        "image_urls", "item_url", "seller_id", "store_url", "category",
        "item_specifics", "mpn", "upc", "shipping", "listing_type", "niche", "scraped_at",
    ]
    for col in expected:
        assert col in columns, f"Missing column: {col}"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
pytest tests/test_export.py -v
```

Expected: `ModuleNotFoundError: No module named 'scraper.export'`

- [ ] **Step 3: Implement `scraper/export.py`**

```python
import csv
import asyncpg
from scraper.db import get_products_by_niche

CSV_COLUMNS = [
    "item_id", "title", "price", "currency", "condition", "description",
    "image_urls", "item_url", "seller_id", "store_url", "category",
    "item_specifics", "mpn", "upc", "shipping", "listing_type", "niche", "scraped_at",
]

async def export_to_csv(
    pool: asyncpg.Pool,
    output_path: str,
    niche: str | None = None,
) -> int:
    rows = await get_products_by_niche(pool, niche)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row))
    return len(rows)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest tests/test_export.py -v
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add scraper/export.py tests/test_export.py
git commit -m "feat: CSV export module"
```

---

## Task 9: CLI

**Files:**
- Create: `scraper/cli.py`

Wires all modules together with Click. No unit tests - logic is already tested in the modules it calls.

- [ ] **Step 1: Implement `scraper/cli.py`**

```python
import asyncio
import asyncpg
import click
from scraper.config import Settings
from scraper.db import init_schema, get_counts, clear_niche
from scraper.queue import get_redis, get_queue, enqueue_items
from scraper.store import get_item_ids_from_store
from scraper.export import export_to_csv
from scraper.worker import start_worker

@click.group()
def cli():
    pass

@cli.command()
@click.argument("source")
@click.option("--niche", required=True, help="Tag for this batch (e.g. car-accessories)")
def add(source: str, niche: str):
    """Add a store URL or text file of URLs to the scrape queue."""
    settings = Settings()

    if source.startswith("http"):
        store_urls = [source]
    else:
        with open(source) as f:
            store_urls = [line.strip() for line in f if line.strip()]

    redis_conn = get_redis(settings.redis_url)
    queue = get_queue(redis_conn)
    total = 0

    for store_url in store_urls:
        click.echo(f"Fetching item IDs from {store_url} ...")
        item_ids = get_item_ids_from_store(
            store_url,
            proxy_url=settings.proxy_url,
            requests_per_second=settings.requests_per_second,
        )
        count = enqueue_items(queue, redis_conn, item_ids, niche=niche, store_url=store_url)
        click.echo(f"  {count} new items queued ({len(item_ids) - count} already seen)")
        total += count

    click.echo(f"Done. {total} total items added to queue.")

@cli.group()
def worker():
    """Worker commands."""
    pass

@worker.command("start")
def worker_start():
    """Start a scrape worker on this VPS (runs until Ctrl+C)."""
    settings = Settings()
    click.echo("Starting worker. Press Ctrl+C to stop.")
    start_worker(settings.redis_url)

@cli.command()
@click.option("--output", default="products.csv", show_default=True)
@click.option("--niche", default=None, help="Filter by niche tag (omit for all)")
def export(output: str, niche: str | None):
    """Export scraped products to a CSV file."""
    settings = Settings()

    async def _run():
        pool = await asyncpg.create_pool(settings.database_url)
        try:
            count = await export_to_csv(pool, output_path=output, niche=niche)
            click.echo(f"Exported {count} products to {output}")
        finally:
            await pool.close()

    asyncio.run(_run())

@cli.command()
def status():
    """Show queue depth and per-niche scraped counts."""
    settings = Settings()
    redis_conn = get_redis(settings.redis_url)
    queue = get_queue(redis_conn)

    async def _run():
        pool = await asyncpg.create_pool(settings.database_url)
        try:
            counts = await get_counts(pool)
            total = sum(counts.values())
            click.echo(f"Queued jobs:   {len(queue)}")
            click.echo(f"Total scraped: {total}")
            if counts:
                click.echo("\nPer-niche:")
                for n, c in sorted(counts.items()):
                    click.echo(f"  {n}: {c}")
        finally:
            await pool.close()

    asyncio.run(_run())

@cli.group()
def db():
    """Database management (run on coordinator only)."""
    pass

@db.command("init")
def db_init():
    """Create the database schema."""
    settings = Settings()

    async def _run():
        pool = await asyncpg.create_pool(settings.database_url)
        try:
            await init_schema(pool)
            click.echo("Schema created.")
        finally:
            await pool.close()

    asyncio.run(_run())

@cli.command()
@click.option("--niche", required=True, help="Niche tag to clear")
@click.confirmation_option(prompt="Remove all scraped data for this niche?")
def clear(niche: str):
    """Remove all scraped records for a niche."""
    settings = Settings()

    async def _run():
        pool = await asyncpg.create_pool(settings.database_url)
        try:
            await clear_niche(pool, niche)
            click.echo(f"Cleared niche: {niche}")
        finally:
            await pool.close()

    asyncio.run(_run())
```

- [ ] **Step 2: Verify CLI entry point loads**

```bash
scraper --help
```

Expected output includes commands: `add`, `clear`, `db`, `export`, `status`, `worker`

- [ ] **Step 3: Verify each command shows help**

```bash
scraper add --help && scraper worker --help && scraper export --help && scraper status --help && scraper db --help && scraper clear --help
```

Expected: each prints help text and exits 0.

- [ ] **Step 4: Commit**

```bash
git add scraper/cli.py
git commit -m "feat: CLI commands"
```

---

## Task 10: Integration Test + README

**Files:**
- Create: `tests/test_integration.py`
- Create: `README.md`

- [ ] **Step 1: Write integration test**

```python
# tests/test_integration.py
import pytest
import respx
import httpx
from scraper.db import get_products_by_niche
from scraper.worker import scrape_and_store

ITEM_HTML = """
<html>
<head>
<script type="application/ld+json">
{
  "@type": "Product",
  "name": "Integration Test Widget",
  "description": "A widget",
  "image": ["https://i.ebayimg.com/test.jpg"],
  "offers": {
    "@type": "Offer",
    "price": "19.99",
    "priceCurrency": "USD",
    "itemCondition": "https://schema.org/NewCondition",
    "seller": {"@type": "Person", "name": "widgetseller"}
  },
  "category": "Widgets"
}
</script>
</head><body></body></html>
"""

TEST_DB_URL = "postgresql://scraper:scraper@localhost:5432/ebayscraper_test"

@respx.mock
def test_scrape_and_store_full_flow(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("DATABASE_URL", TEST_DB_URL)
    monkeypatch.setenv("PROXY_URL", "")

    respx.get("https://www.ebay.com/itm/777777777").mock(
        return_value=httpx.Response(200, text=ITEM_HTML)
    )

    scrape_and_store("777777777", "integration-test", "https://www.ebay.com/str/teststore")

    import asyncio
    import asyncpg
    from scraper.db import init_schema

    async def check():
        pool = await asyncpg.create_pool(TEST_DB_URL)
        await init_schema(pool)
        results = await get_products_by_niche(pool, "integration-test")
        await pool.execute("DELETE FROM products WHERE niche = 'integration-test'")
        await pool.close()
        return results

    results = asyncio.run(check())
    assert len(results) == 1
    assert results[0]["item_id"] == "777777777"
    assert results[0]["title"] == "Integration Test Widget"
    assert float(results[0]["price"]) == 19.99
    assert results[0]["niche"] == "integration-test"
```

- [ ] **Step 2: Run integration test**

```bash
pytest tests/test_integration.py -v
```

Expected: 1 passed.

- [ ] **Step 3: Run full test suite**

```bash
pytest -v
```

Expected: all tests pass.

- [ ] **Step 4: Create `README.md`**

```markdown
# eBay Store Scraper

Distributed eBay product scraper. Point it at seller store URLs, run workers across multiple VPS nodes, export everything to CSV.

## Requirements

- Python 3.11+
- Docker (for coordinator Redis + Postgres)
- Rotating residential proxy (optional, required for 1M+/day scale)

## Coordinator Setup (one VPS, one-time)

```bash
git clone <repo> && cd ebay-scraper
pip install -r requirements.txt && pip install -e .
cp .env.example .env        # edit with your settings
docker compose up -d        # starts Redis + Postgres
scraper db init             # creates schema
```

## Worker Setup (repeat on each additional VPS)

```bash
git clone <repo> && cd ebay-scraper
pip install -r requirements.txt && pip install -e .
cp .env.example .env
# Set REDIS_URL and DATABASE_URL to point at coordinator IP
scraper worker start
```

## Usage

```bash
# Add a single store
scraper add https://www.ebay.com/str/somestore --niche car-accessories

# Add stores from a file (one URL per line)
scraper add stores.txt --niche car-accessories

# Check progress
scraper status

# Export to CSV
scraper export --output products.csv --niche car-accessories

# Clear a niche (removes DB records, allows re-scraping)
scraper clear --niche car-accessories
```

## Config (`.env`)

| Variable | Required | Default | Description |
|---|---|---|---|
| `REDIS_URL` | Yes | | Redis connection string |
| `DATABASE_URL` | Yes | | Postgres connection string |
| `PROXY_URL` | No | | HTTP proxy for scraping (rotating residential recommended) |
| `REQUESTS_PER_SECOND` | No | `0.5` | Rate limit per worker |

## Scale

Each worker VPS does ~50k-200k items/day with a rotating residential proxy.
Run 5-10 worker VPS nodes to hit ~1M items/day.
All nodes share the same coordinator Redis and Postgres.
```

- [ ] **Step 5: Final commit**

```bash
git add tests/test_integration.py README.md
git commit -m "test: integration test and deployment README"
```

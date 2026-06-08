import os
import asyncio
import csv

import asyncpg

from scraper.db import get_products_by_niche, init_schema
from scraper.export import export_to_csv
from scraper.worker import scrape_batch

ITEM_HTML = """
<html>
<head>
<script type="application/ld+json">
{
  "@type": "Product",
  "name": "Integration Test Widget",
  "description": "A widget",
  "image": ["https://i.ebayimg.com/test1.jpg", "https://i.ebayimg.com/test2.jpg"],
  "offers": {
    "@type": "Offer",
    "price": "19.99",
    "priceCurrency": "USD",
    "itemCondition": "https://schema.org/NewCondition"
  },
  "category": "Widgets"
}
</script>
</head>
<body>
<div class="ux-layout-section--features">
  <dl><dt>Brand</dt><dd>Acme</dd><dt>MPN</dt><dd>WIDGET-1</dd></dl>
</div>
<iframe src="https://itm.ebaydesc.com/itmdesc/777777777"></iframe>
</body></html>
"""

DESC_HTML = "<html><body>Full widget description here.</body></html>"

TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql://scraper:scraper@localhost:5432/ebayscraper_test",
)


class _FakeResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeSession:
    """Stand-in for a curl_cffi session that serves canned eBay pages.

    scrape_item builds its HTTP session via scraper.fetch.build_session; patching
    that lets the integration test exercise the real parsing, DB-write and export
    path without hitting the network (curl_cffi cannot be intercepted by respx).
    """

    def get(self, url: str, **kwargs) -> _FakeResponse:
        if "ebaydesc.com" in url:
            return _FakeResponse(200, DESC_HTML)
        if "/itm/" in url:
            return _FakeResponse(200, ITEM_HTML)
        return _FakeResponse(200, "<html></html>")

    def close(self) -> None:
        pass


def test_scrape_store_export_full_flow(monkeypatch, tmp_path):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("DATABASE_URL", TEST_DB_URL)
    monkeypatch.setenv("PROXY_URL", "")
    monkeypatch.setenv("REQUESTS_PER_SECOND", "0")
    monkeypatch.setattr("scraper.worker.build_session", lambda proxy=None: _FakeSession())

    item_url = "https://www.ebay.com/itm/777777777"
    store_url = "https://www.ebay.com/str/teststore"

    scrape_batch([item_url], "integration-test", store_url)

    async def check_and_export() -> tuple:
        pool = await asyncpg.create_pool(TEST_DB_URL)
        await init_schema(pool)
        results = await get_products_by_niche(pool, "integration-test")
        csv_path = tmp_path / "out.csv"
        count = await export_to_csv(pool, output_path=str(csv_path), niche="integration-test")
        await pool.execute("DELETE FROM products WHERE niche = 'integration-test'")
        await pool.close()
        return results, csv_path, count

    results, csv_path, count = asyncio.run(check_and_export())

    assert len(results) == 1
    row = results[0]
    assert row["item_id"] == "777777777"
    assert row["title"] == "Integration Test Widget"
    assert float(row["price"]) == 19.99
    assert row["niche"] == "integration-test"
    # seller is absent from the JSON-LD, so it must be derived from the store URL.
    assert row["seller_id"] == "teststore"
    assert row["image_urls"] == "https://i.ebayimg.com/test1.jpg|https://i.ebayimg.com/test2.jpg"
    assert row["mpn"] == "WIDGET-1"
    assert "Full widget description" in row["description"]

    assert count == 1
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["item_id"] == "777777777"
    assert rows[0]["seller_id"] == "teststore"
    assert rows[0]["title"] == "Integration Test Widget"

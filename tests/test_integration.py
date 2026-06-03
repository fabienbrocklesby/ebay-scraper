import asyncio
import pytest
import respx
import httpx
import asyncpg

from scraper.db import get_products_by_niche, init_schema
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

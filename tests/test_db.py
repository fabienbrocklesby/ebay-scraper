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
        mpn="",
        upc="",
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

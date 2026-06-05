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
        mpn="",
        upc="",
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
async def test_export_split_csv_chunks_by_rows(db_pool, tmp_path):
    from scraper.db import insert_product
    from scraper.export import export_split_csv
    for i in range(5):
        await insert_product(db_pool, make_record(item_id=f"s{i}", niche="split"))
    paths = await export_split_csv(
        db_pool, output_dir=str(tmp_path), rows_per_file=2, niche="split"
    )
    assert len(paths) == 3  # 2 + 2 + 1
    import csv as csv_mod
    total = 0
    for p in paths:
        with open(p) as f:
            rows = list(csv_mod.DictReader(f))
            total += len(rows)
            assert "item_id" in rows[0]  # header present in every file
    assert total == 5


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

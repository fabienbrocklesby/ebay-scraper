import csv

import asyncpg

from scraper.db import get_products_by_niche

CSV_COLUMNS = [
    "item_id",
    "title",
    "price",
    "currency",
    "condition",
    "description",
    "image_urls",
    "item_url",
    "seller_id",
    "store_url",
    "category",
    "item_specifics",
    "mpn",
    "upc",
    "shipping",
    "listing_type",
    "niche",
    "scraped_at",
]


async def export_to_csv(
    pool: asyncpg.Pool,
    output_path: str,
    niche: str | None = None,
) -> int:
    """Query products from Postgres and write them to a CSV file.

    Args:
        pool: Active asyncpg connection pool.
        output_path: Filesystem path for the output CSV file.
        niche: If provided, only export products with this niche tag.
               If None, export all products.

    Returns:
        Number of rows written.
    """
    rows = await get_products_by_niche(pool, niche)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(dict(row))
    return len(rows)

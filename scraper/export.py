import csv
import os

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


def _record_to_row(record) -> list:
    """Serialize an asyncpg Record (or dict-like) into a CSV row list.

    Field order matches CSV_COLUMNS exactly. None values are rendered as empty
    strings (not the literal string "None"). This is the correct behaviour for
    Shopify CSV imports, where a "None" string would be treated as a value.
    """
    d = dict(record)
    return [("" if d.get(col) is None else d[col]) for col in CSV_COLUMNS]


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
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)
        for row in rows:
            writer.writerow(_record_to_row(row))
    return len(rows)


async def export_split_csv(
    pool: asyncpg.Pool,
    output_dir: str,
    rows_per_file: int = 500_000,
    niche: str | None = None,
) -> list[str]:
    """Export all products into numbered CSV files capped at rows_per_file rows each.

    Args:
        pool: Active asyncpg connection pool.
        output_dir: Directory where output files are written.
        rows_per_file: Maximum data rows per file (default 500,000).
        niche: If provided, only export products with this niche tag.
               If None, export all products.

    Returns:
        List of absolute paths to the written CSV files, in order.
    """
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

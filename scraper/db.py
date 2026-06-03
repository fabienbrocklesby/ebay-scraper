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
    await pool.execute(
        """
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

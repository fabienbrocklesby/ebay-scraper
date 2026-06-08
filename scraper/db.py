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
            scraped_at   TIMESTAMPTZ DEFAULT now(),
            last_seen_at TIMESTAMPTZ DEFAULT now(),
            is_active    BOOLEAN DEFAULT true
        )
    """)
    await pool.execute(
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ DEFAULT now()"
    )
    await pool.execute(
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT true"
    )
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS stores (
            store_url TEXT PRIMARY KEY,
            niche     TEXT NOT NULL DEFAULT '',
            added_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    await pool.execute("ALTER TABLE stores ADD COLUMN IF NOT EXISTS marketplace_domain  TEXT")
    await pool.execute("ALTER TABLE stores ADD COLUMN IF NOT EXISTS marketplace_country TEXT")
    await pool.execute("ALTER TABLE stores ADD COLUMN IF NOT EXISTS detected_at TIMESTAMPTZ")


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


async def get_recent_products(pool: asyncpg.Pool, limit: int = 15) -> list[asyncpg.Record]:
    """Most recently scraped products, newest first, for the live monitor feed."""
    return await pool.fetch(
        "SELECT item_id, title, price, currency, store_url, last_seen_at "
        "FROM products ORDER BY last_seen_at DESC LIMIT $1",
        limit,
    )


async def clear_niche(pool: asyncpg.Pool, niche: str) -> None:
    await pool.execute("DELETE FROM products WHERE niche = $1", niche)


async def add_store(pool: asyncpg.Pool, store_url: str, niche: str = "") -> None:
    await pool.execute(
        """
        INSERT INTO stores (store_url, niche)
        VALUES ($1, $2)
        ON CONFLICT (store_url) DO UPDATE SET niche = EXCLUDED.niche
        """,
        store_url, niche,
    )


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


async def list_stores(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    return await pool.fetch(
        """
        SELECT store_url, niche, added_at, marketplace_domain, marketplace_country, detected_at
          FROM stores
         ORDER BY added_at
        """
    )


async def remove_store(pool: asyncpg.Pool, store_url: str) -> bool:
    result = await pool.execute("DELETE FROM stores WHERE store_url = $1", store_url)
    return result.split()[-1] == "1"


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
    return int(result.split()[-1])

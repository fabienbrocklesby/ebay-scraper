import psycopg2
import time
from datetime import datetime, timezone
from typing import Optional

from psycopg2.extras import execute_values

from scraper.config import Settings
from scraper.queue import PROXY_REDIS_KEY
from scraper.scraper import scrape_item, ProductData
from scraper.store import extract_seller_id


def _get_proxy_url(settings: Settings) -> str | None:
    """Read proxy from Redis (coordinator-managed) with fallback to local config.

    An empty-string Redis value means proxy was explicitly cleared by the coordinator.
    """
    import redis as redis_lib
    conn = redis_lib.from_url(settings.redis_url)
    raw = conn.get(PROXY_REDIS_KEY)
    if raw is not None:
        val = raw.decode().strip()
        return val if val else None
    return settings.proxy_url

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


_BULK_COLUMNS = (
    "item_id, title, price, currency, condition, description, image_urls, item_url, "
    "seller_id, store_url, category, item_specifics, mpn, upc, shipping, listing_type, "
    "niche, last_seen_at, is_active"
)

_BULK_UPSERT_SQL = f"""
    INSERT INTO products ({_BULK_COLUMNS})
    VALUES %s
    ON CONFLICT (item_id) DO UPDATE SET
        title = EXCLUDED.title,
        price = EXCLUDED.price,
        currency = EXCLUDED.currency,
        condition = EXCLUDED.condition,
        description = EXCLUDED.description,
        image_urls = EXCLUDED.image_urls,
        item_url = EXCLUDED.item_url,
        seller_id = EXCLUDED.seller_id,
        store_url = EXCLUDED.store_url,
        category = EXCLUDED.category,
        item_specifics = EXCLUDED.item_specifics,
        mpn = EXCLUDED.mpn,
        upc = EXCLUDED.upc,
        shipping = EXCLUDED.shipping,
        listing_type = EXCLUDED.listing_type,
        niche = EXCLUDED.niche,
        last_seen_at = now(),
        is_active = true
"""

_BULK_TEMPLATE = (
    "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now(), true)"
)


def _bulk_upsert(database_url: str, products: list[ProductData], niche: str, store_url: str) -> None:
    rows = [
        (
            p.item_id, p.title, p.price, p.currency, p.condition, p.description,
            p.image_urls, p.item_url, p.seller_id, store_url, p.category,
            p.item_specifics, p.mpn, p.upc, p.shipping, p.listing_type, niche,
        )
        for p in products
    ]
    conn = psycopg2.connect(database_url)
    try:
        with conn.cursor() as cur:
            execute_values(cur, _BULK_UPSERT_SQL, rows, template=_BULK_TEMPLATE, page_size=200)
        conn.commit()
    finally:
        conn.close()


def scrape_and_store(item_url: str, niche: str, store_url: str) -> None:
    """Scrape a single eBay item and persist it to Postgres.

    Accepts the full item URL (e.g. https://www.ebay.com.au/itm/123) so the
    correct eBay domain is used for non-US stores.

    Intended to be enqueued as an rq job. Uses psycopg2 (sync) deliberately:
    rq worker processes are synchronous, and mixing asyncpg into a sync context
    would require creating a new event loop per job, which is fragile.
    """
    settings = Settings()
    # Enforce rate limit between jobs so a single proxy IP isn't volume-flagged.
    # scrape_item already sleeps 1s internally; this adds the remainder.
    if settings.requests_per_second > 0:
        time.sleep(max(0.0, (1.0 / settings.requests_per_second) - 1.0))
    product: Optional[ProductData] = scrape_item(item_url, proxy_url=_get_proxy_url(settings))
    if product is None:
        return

    # eBay's item JSON-LD often omits the seller, but an item in a store belongs
    # to that store's seller, so derive it from the store URL when it is missing.
    if not product.seller_id:
        try:
            product.seller_id = extract_seller_id(store_url)
        except ValueError:
            pass

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


def crawl_store(store_url: str, niche: str) -> None:
    """Paginate a store page and enqueue individual item scrape jobs.

    Runs on VPS workers. Uses late imports to avoid circular dependency with queue.py.
    """
    from scraper.queue import get_redis, get_queue, enqueue_items
    from scraper.store import get_item_urls_from_store

    settings = Settings()
    item_urls = get_item_urls_from_store(
        store_url,
        proxy_url=_get_proxy_url(settings),
        requests_per_second=settings.requests_per_second,
    )
    conn = get_redis(settings.redis_url)
    queue = get_queue(conn)
    enqueue_items(queue, conn, item_urls, niche=niche, store_url=store_url)


def start_worker(redis_url: str) -> None:
    """Start an rq worker listening on the 'scraping' queue."""
    import redis as redis_lib
    from rq import Worker

    conn = redis_lib.from_url(redis_url)
    worker = Worker(["scraping"], connection=conn)
    worker.work(with_scheduler=True)

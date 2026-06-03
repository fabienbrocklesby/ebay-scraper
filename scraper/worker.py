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


def scrape_and_store(item_url: str, niche: str, store_url: str) -> None:
    """Scrape a single eBay item and persist it to Postgres.

    Accepts the full item URL (e.g. https://www.ebay.com.au/itm/123) so the
    correct eBay domain is used for non-US stores.

    Intended to be enqueued as an rq job. Uses psycopg2 (sync) deliberately:
    rq worker processes are synchronous, and mixing asyncpg into a sync context
    would require creating a new event loop per job, which is fragile.
    """
    settings = Settings()
    product: Optional[ProductData] = scrape_item(item_url, proxy_url=settings.proxy_url)
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
    """Start an rq worker listening on the 'scraping' queue."""
    import redis as redis_lib
    from rq import Worker

    conn = redis_lib.from_url(redis_url)
    worker = Worker(["scraping"], connection=conn)
    worker.work(with_scheduler=True)

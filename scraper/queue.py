import re
import redis as redis_lib
from rq import Queue, Retry

SCRAPED_SET_KEY = "scraped_items"
PROXY_REDIS_KEY = "ebay-scraper:proxy_url"

# A scrape job that hits eBay's bot challenge raises rather than dropping the
# item. These retries requeue it on an escalating delay so a later attempt (on a
# fresh proxy IP, or after the per-IP cooldown) can succeed. Non-zero intervals
# require the worker to run with the scheduler enabled (worker.work does).
_JOB_RETRY = Retry(max=3, interval=[120, 300, 900])

_ITEM_ID_RE = re.compile(r"/itm/(\d+)")


def _item_id_from_url(item_url: str) -> str:
    m = _ITEM_ID_RE.search(item_url)
    if not m:
        raise ValueError(f"Cannot extract item ID from URL: {item_url}")
    return m.group(1)


def get_redis(redis_url: str) -> redis_lib.Redis:
    return redis_lib.from_url(redis_url)


def get_queue(redis_conn: redis_lib.Redis) -> Queue:
    return Queue("scraping", connection=redis_conn)


def is_item_queued(redis_conn: redis_lib.Redis, item_id: str) -> bool:
    return bool(redis_conn.sismember(SCRAPED_SET_KEY, item_id))


def mark_item_queued(redis_conn: redis_lib.Redis, item_id: str) -> None:
    redis_conn.sadd(SCRAPED_SET_KEY, item_id)


def enqueue_items(
    queue: Queue,
    redis_conn: redis_lib.Redis,
    item_urls: list[str],
    niche: str,
    store_url: str,
) -> int:
    """Enqueue item URLs for scraping, skipping already-queued items.

    Deduplication is keyed on item_id (extracted from the URL) so the same
    item is not scraped twice even if it appears in multiple stores.
    """
    from scraper.worker import scrape_and_store  # late import: worker imports queue, avoid circular
    enqueued = 0
    for item_url in item_urls:
        item_id = _item_id_from_url(item_url)
        if is_item_queued(redis_conn, item_id):
            continue
        queue.enqueue(
            scrape_and_store,
            item_url,
            niche,
            store_url,
            job_timeout=300,
            retry=_JOB_RETRY,
        )
        mark_item_queued(redis_conn, item_id)
        enqueued += 1
    return enqueued


def enqueue_crawl_job(queue: Queue, store_url: str, niche: str) -> None:
    """Enqueue a store-crawl job. Workers paginate the store and queue item jobs."""
    from scraper.worker import crawl_store  # late import: same circular-avoidance reason
    queue.enqueue(crawl_store, store_url, niche, job_timeout=3600)

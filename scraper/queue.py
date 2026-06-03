import redis as redis_lib
from rq import Queue

from scraper.worker import scrape_and_store

SCRAPED_SET_KEY = "scraped_items"


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
    item_ids: list[str],
    niche: str,
    store_url: str,
) -> int:
    enqueued = 0
    for item_id in item_ids:
        if is_item_queued(redis_conn, item_id):
            continue
        queue.enqueue(
            scrape_and_store,
            item_id,
            niche,
            store_url,
            job_timeout=300,
        )
        mark_item_queued(redis_conn, item_id)
        enqueued += 1
    return enqueued

import re
import redis as redis_lib
from rq import Queue, Retry
from rq.registry import StartedJobRegistry, DeferredJobRegistry, ScheduledJobRegistry

SCRAPED_SET_KEY = "scraped_items"
PROXY_REDIS_KEY = "ebay-scraper:proxy_url"
# A Redis set of static ISP proxy URLs used for item-detail fetching. Discovery
# (store pagination) uses the rotating proxy at PROXY_REDIS_KEY; the bulk item
# fetches are spread across this flat-rate pool. Adding a member here makes the
# next batch start using that IP, so the pool scales live with no restart.
ISP_POOL_REDIS_KEY = "ebay-scraper:isp_pool"

# A scrape job that hits eBay's bot challenge raises rather than dropping the
# item. These retries requeue it on an escalating delay so a later attempt (on a
# fresh proxy IP, or after the per-IP cooldown) can succeed. Non-zero intervals
# require the worker to run with the scheduler enabled (worker.work does).
_JOB_RETRY = Retry(max=3, interval=[120, 300, 900])

_ITEM_ID_RE = re.compile(r"/itm/(\d+)")

_BATCH_SIZE = 200


def _item_id_from_url(item_url: str) -> str:
    m = _ITEM_ID_RE.search(item_url)
    if not m:
        raise ValueError(f"Cannot extract item ID from URL: {item_url}")
    return m.group(1)


def resolve_proxy(redis_conn, settings) -> str | None:
    """Resolve the active proxy URL. Redis is authoritative: a present value (even an
    empty string, meaning 'explicitly cleared') overrides local config; only a missing
    key falls back to settings.proxy_url."""
    raw = redis_conn.get(PROXY_REDIS_KEY)
    if raw is not None:
        value = raw.decode().strip()
        return value or None
    return settings.proxy_url or None


def normalize_proxy_url(raw: str) -> str:
    """Normalize a proxy string into a full URL the HTTP client accepts.

    Accepts the IPRoyal copy-paste format `host:port:user:pass`, a bare
    `host:port`, or an already-complete `http(s)://user:pass@host:port`. Returns
    `http://user:pass@host:port` (or `http://host:port` when no credentials).
    Raises ValueError on anything that is not recognisably one of these so a typo
    never gets silently stored as an unusable pool member.
    """
    value = raw.strip()
    if not value:
        raise ValueError("empty proxy string")
    if "://" in value:
        return value
    parts = value.split(":")
    if len(parts) == 2:
        host, port = parts
    elif len(parts) == 4:
        host, port, user, password = parts
        return f"http://{user}:{password}@{host}:{port}"
    else:
        raise ValueError(
            f"unrecognised proxy format: expected host:port, host:port:user:pass, "
            f"or a full URL, got {raw!r}"
        )
    if not (host and port.isdigit()):
        raise ValueError(f"unrecognised proxy format: {raw!r}")
    return f"http://{host}:{port}"


def resolve_isp_pool(redis_conn) -> list[str]:
    """Return the configured static ISP proxy pool, sorted for deterministic order.

    Members are stored already-normalized (see normalize_proxy_url). Sorting keeps
    the per-batch round-robin assignment stable across workers reading the same set.
    """
    members = redis_conn.smembers(ISP_POOL_REDIS_KEY)
    return sorted(m.decode() if isinstance(m, bytes) else m for m in members)


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
    """Enqueue item URLs as batched scrape_batch jobs, skipping already-queued items.

    Deduplication is keyed on item_id (extracted from the URL) so the same item is
    not scraped twice even across stores. New URLs are chunked into batches of
    _BATCH_SIZE and each batch becomes one scrape_batch job.
    """
    from scraper.worker import scrape_batch  # late import: worker imports queue, avoid circular

    new_urls: list[str] = []
    for item_url in item_urls:
        item_id = _item_id_from_url(item_url)
        if is_item_queued(redis_conn, item_id):
            continue
        mark_item_queued(redis_conn, item_id)
        new_urls.append(item_url)

    enqueued = 0
    for i in range(0, len(new_urls), _BATCH_SIZE):
        batch = new_urls[i : i + _BATCH_SIZE]
        queue.enqueue(scrape_batch, batch, niche, store_url, 0, job_timeout=600, retry=_JOB_RETRY)
        enqueued += len(batch)
    return enqueued


def queue_is_drained(queue: Queue) -> bool:
    """True when no jobs are pending, running, deferred (awaiting retry), or scheduled."""
    started = StartedJobRegistry(queue=queue)
    deferred = DeferredJobRegistry(queue=queue)
    scheduled = ScheduledJobRegistry(queue=queue)
    return (
        queue.count == 0
        and started.count == 0
        and deferred.count == 0
        and scheduled.count == 0
    )


def enqueue_crawl_job(queue: Queue, store_url: str, niche: str) -> None:
    """Enqueue a store-crawl job. Workers paginate the store and queue item jobs."""
    from scraper.worker import crawl_store  # late import: same circular-avoidance reason
    queue.enqueue(crawl_store, store_url, niche, job_timeout=3600)

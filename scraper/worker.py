import psycopg2
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional
from urllib.parse import urlparse

from psycopg2.extras import execute_values

from scraper.config import Settings
from scraper.fetch import ChallengeError, WrongCountryError, build_session, apply_proxy_country
from scraper.queue import PROXY_REDIS_KEY, resolve_proxy
from scraper.scraper import scrape_item, parse_item_html, ProductData
from scraper.store import extract_seller_id
from scraper.throttle import TokenBucket, BoxProxyState
from scraper.unblocker import UnblockerConfig, fetch_via_unblocker, load_unblocker_config


def _get_proxy_url(settings: Settings) -> str | None:
    """Read proxy from Redis (coordinator-managed) with fallback to local config.

    Delegates to resolve_proxy so worker and coordinator share one definition.
    """
    import redis as redis_lib
    conn = redis_lib.from_url(settings.redis_url)
    return resolve_proxy(conn, settings)


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


_session_local = threading.local()


def _warmed_session(proxy_url: str | None, item_url: str) -> Any:
    """Return a per-thread curl_cffi session, warmed once against the eBay homepage.

    eBay serves a 403 to a cold session that carries no cookies. scrape_item's own
    client path warms via a homepage GET, but the batched worker injects its own
    session to skip the per-item warmup and 1s sleep, so it must warm the session
    itself. The warmed session is cached per (proxy, host) on the worker thread and
    reused for every item, so the homepage cost is paid once per thread, not per item.
    """
    cache = getattr(_session_local, "sessions", None)
    if cache is None:
        cache = {}
        _session_local.sessions = cache
    host = urlparse(item_url).netloc
    key = (proxy_url or "", host)
    session = cache.get(key)
    if session is None:
        session = build_session(apply_proxy_country(proxy_url, item_url))
        try:
            session.get(f"https://{host}/", timeout=30)
        except Exception:
            pass
        cache[key] = session
    return session


def _scrape_one(item_url: str, residential_proxy: str | None, box_state: BoxProxyState, bucket: TokenBucket) -> Optional[ProductData]:
    """Fetch one item. Box IP first; on challenge or wrong country, retry via residential.

    A warmed per-thread session is injected, which makes scrape_item skip its own
    homepage warmup and 1s sleep, so the TokenBucket is the only rate governor. A
    datacenter box IP works once the session is warmed; if eBay still blocks it
    (403 or challenge), that raises and we retry via the residential proxy. Raises
    if the residential retry also fails so the caller can mark the item failed.
    """
    bucket.acquire()
    use_residential = bool(box_state.should_use_residential() and residential_proxy)
    proxy = residential_proxy if use_residential else None
    try:
        session = _warmed_session(proxy, item_url)
        data = scrape_item(item_url, proxy_url=proxy, client=session)
        box_state.record(challenged=False)
        return data
    except (ChallengeError, WrongCountryError):
        box_state.record(challenged=True)
        if residential_proxy and not use_residential:
            bucket.acquire()
            session = _warmed_session(residential_proxy, item_url)
            return scrape_item(item_url, proxy_url=residential_proxy, client=session)
        raise


def _scrape_one_with_unblocker(
    item_url: str,
    residential_proxy: str | None,
    box_state: BoxProxyState,
    bucket: TokenBucket,
    unblocker_config: UnblockerConfig,
    redis_conn: Any,
) -> Optional[ProductData]:
    """Attempt _scrape_one and escalate to the paid unblocker on a persistent challenge.

    A None return from _scrape_one (genuine 404 or unparseable page) passes through
    without touching the unblocker: there is nothing to fetch and the paid call would
    be wasted. Only a ChallengeError (which WrongCountryError subclasses) triggers
    escalation. When the unblocker is disabled the exception is re-raised so
    scrape_batch's existing requeue logic handles it unchanged.

    The description iframe is not fetched on the unblocker path because the unblocker
    returns a standalone HTML snapshot with no live session cookies. The description
    field will be empty for these tail escalations, which is an acceptable tradeoff
    versus abandoning the item entirely.

    The unblocker path also skips the wrong-country currency guard (proxy_url=None is
    passed to parse_item_html). This is acceptable: Oxylabs Web Unlocker pins
    geo_location to the target country, so the HTML it returns is already geo-correct.
    """
    try:
        return _scrape_one(item_url, residential_proxy, box_state, bucket)
    except ChallengeError:
        if unblocker_config is None or not unblocker_config.enabled:
            raise
        html = fetch_via_unblocker(item_url, unblocker_config, redis_conn)
        if not html:
            raise
        return parse_item_html(html, item_url)


_MAX_BATCH_ATTEMPTS = 3


def scrape_batch(item_urls: list[str], niche: str, store_url: str, attempt: int = 0) -> None:
    """rq job: fetch a batch of item URLs concurrently and bulk-upsert the results.

    Failed items are collected and re-enqueued as a smaller batch, up to
    _MAX_BATCH_ATTEMPTS total attempts, so a few bad items never fail the whole
    batch and never loop forever.

    eBay's item JSON-LD often omits the seller, but an item in a store belongs
    to that store's seller, so seller_id is derived from the store URL when missing.
    """
    settings = Settings()
    residential = _get_proxy_url(settings)
    bucket = TokenBucket(settings.max_rps_per_ip)
    box_state = BoxProxyState(
        settings.challenge_escalation_threshold, settings.challenge_cooldown_seconds
    )

    from scraper.queue import get_redis
    redis_conn = get_redis(settings.redis_url)
    unblocker_config = load_unblocker_config(redis_conn)

    results: list[ProductData] = []
    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=settings.worker_concurrency) as ex:
        futures = {
            ex.submit(_scrape_one_with_unblocker, url, residential, box_state, bucket, unblocker_config, redis_conn): url
            for url in item_urls
        }
        for fut in as_completed(futures):
            url = futures[fut]
            try:
                data = fut.result()
                if data is not None:
                    if not data.seller_id:
                        try:
                            data.seller_id = extract_seller_id(store_url)
                        except ValueError:
                            pass
                    results.append(data)
            except Exception:
                failed.append(url)

    if results:
        _bulk_upsert(settings.database_url, results, niche, store_url)
    if failed and attempt + 1 < _MAX_BATCH_ATTEMPTS:
        _requeue_failed(failed, niche, store_url, attempt + 1)


def _requeue_failed(item_urls: list[str], niche: str, store_url: str, attempt: int) -> None:
    from scraper.queue import get_redis, get_queue
    settings = Settings()
    conn = get_redis(settings.redis_url)
    queue = get_queue(conn)
    queue.enqueue(scrape_batch, item_urls, niche, store_url, attempt, job_timeout=600)


def crawl_store(store_url: str, niche: str) -> None:
    """Paginate a store page and enqueue item scrape jobs.

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
    import os
    import sys
    import redis as redis_lib
    from rq import Worker

    # rq forks a work-horse per job. On macOS, forking after the Objective-C
    # runtime has initialized aborts with an NSCharacterSet fork-safety error,
    # which kills every job. This env var restores the pre-Catalina fork behaviour
    # and is the standard fix; it is a no-op on Linux, where workers normally run.
    if sys.platform == "darwin":
        os.environ.setdefault("OBJC_DISABLE_INITIALIZE_FORK_SAFETY", "YES")

    conn = redis_lib.from_url(redis_url)
    worker = Worker(["scraping"], connection=conn)
    worker.work(with_scheduler=True)

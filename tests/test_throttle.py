import time
from scraper.throttle import TokenBucket


def test_token_bucket_caps_rate():
    bucket = TokenBucket(rate_per_sec=10.0)
    start = time.monotonic()
    for _ in range(20):
        bucket.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.9


def test_token_bucket_thread_safe():
    import concurrent.futures as cf
    bucket = TokenBucket(rate_per_sec=50.0)
    start = time.monotonic()
    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        list(ex.map(lambda _: bucket.acquire(), range(100)))
    elapsed = time.monotonic() - start
    assert elapsed >= 1.0

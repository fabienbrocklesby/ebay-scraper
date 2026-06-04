import time
from scraper.throttle import TokenBucket, BoxProxyState


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


def test_box_state_escalates_after_challenge_threshold():
    state = BoxProxyState(threshold=0.2, cooldown_seconds=5.0, window=10)
    assert state.should_use_residential() is False
    for _ in range(3):
        state.record(challenged=True)
    assert state.should_use_residential() is True


def test_box_state_recovers_after_cooldown():
    state = BoxProxyState(threshold=0.5, cooldown_seconds=0.2, window=4)
    state.record(challenged=True)
    state.record(challenged=True)
    assert state.should_use_residential() is True
    time.sleep(0.25)
    assert state.should_use_residential() is False

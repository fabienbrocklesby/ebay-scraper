from unittest.mock import MagicMock, patch
from scraper.scraper import ProductData


def _pd(item_id):
    return ProductData(
        item_id=item_id, title="t", price=1.0, currency="AUD", condition="New",
        description="d", image_urls="u", item_url=f"https://www.ebay.com.au/itm/{item_id}",
        seller_id="s", category="c", item_specifics="{}", mpn="", upc="",
        shipping="", listing_type="FixedPrice",
    )


def test_bulk_upsert_executes_values_once(monkeypatch):
    import scraper.worker as w
    captured = {}

    def fake_execute_values(cur, sql, rows, template=None, page_size=100):
        captured["sql"] = sql
        captured["rows"] = list(rows)

    mock_conn = MagicMock()
    monkeypatch.setattr(w.psycopg2, "connect", lambda dsn: mock_conn)
    monkeypatch.setattr(w, "execute_values", fake_execute_values)

    w._bulk_upsert("postgresql://x/y", [_pd("111111111111"), _pd("222222222222")], "watch", "https://store")

    assert len(captured["rows"]) == 2
    assert "ON CONFLICT (item_id)" in captured["sql"]
    assert "is_active" in captured["sql"]
    assert mock_conn.commit.called


from scraper.fetch import ChallengeError
from scraper.throttle import TokenBucket, BoxProxyState


def test_scrape_one_injects_session_and_escalates_on_challenge(monkeypatch):
    import scraper.worker as w
    calls = []  # (proxy_url, client)

    monkeypatch.setattr(w, "build_session", lambda proxy=None: f"session::{proxy}")

    def fake_scrape_item(item_url, proxy_url=None, client=None):
        calls.append((proxy_url, client))
        if proxy_url is None:
            raise ChallengeError("blocked")
        return _pd("111111111111")

    monkeypatch.setattr(w, "scrape_item", fake_scrape_item)
    bucket = TokenBucket(1000.0)
    state = BoxProxyState(threshold=0.15, cooldown_seconds=10.0)

    result = w._scrape_one(
        "https://www.ebay.com.au/itm/111111111111",
        residential_proxy="http://user:pass@proxy:8080",
        box_state=state, bucket=bucket,
    )
    assert result is not None
    assert [c[0] for c in calls] == [None, "http://user:pass@proxy:8080"]
    assert all(c[1] is not None for c in calls)


def test_scrape_one_returns_none_on_404(monkeypatch):
    import scraper.worker as w
    monkeypatch.setattr(w, "build_session", lambda proxy=None: "session")
    monkeypatch.setattr(w, "scrape_item", lambda *a, **k: None)
    bucket = TokenBucket(1000.0)
    state = BoxProxyState(0.15, 10.0)
    assert w._scrape_one("https://www.ebay.com.au/itm/1", None, state, bucket) is None


def _fake_unblocker_config():
    from scraper.unblocker import UnblockerConfig
    return UnblockerConfig(provider="none", username=None, password=None)


def test_scrape_batch_fetches_concurrently_and_bulk_writes(monkeypatch):
    import scraper.worker as w
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setattr(w, "_get_proxy_url", lambda s: None)
    monkeypatch.setattr(w, "load_unblocker_config", lambda rc: _fake_unblocker_config())

    # get_redis is imported inside scrape_batch via late import; patch at queue level
    import scraper.queue as q
    monkeypatch.setattr(q, "get_redis", lambda url: None)
    monkeypatch.setattr(w, "resolve_isp_pool", lambda conn: [])

    def fake_scrape_one(url, residential, state, bucket):
        if url.endswith("999"):
            raise Exception("boom")
        return _pd(url.rsplit("/", 1)[1])

    written = {}
    requeued = {}
    monkeypatch.setattr(w, "_scrape_one", fake_scrape_one)
    monkeypatch.setattr(w, "_bulk_upsert", lambda db, prods, niche, store: written.update({"n": len(prods)}))
    monkeypatch.setattr(w, "_requeue_failed", lambda urls, niche, store, attempt: requeued.update({"urls": list(urls), "attempt": attempt}))

    urls = [f"https://www.ebay.com.au/itm/{i}" for i in ("111", "222", "999")]
    w.scrape_batch(urls, "watch", "https://store", attempt=0)

    assert written["n"] == 2
    assert requeued["urls"] == ["https://www.ebay.com.au/itm/999"]
    assert requeued["attempt"] == 1


def test_scrape_batch_drops_failures_after_max_attempts(monkeypatch):
    import scraper.worker as w
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setattr(w, "_get_proxy_url", lambda s: None)
    monkeypatch.setattr(w, "load_unblocker_config", lambda rc: _fake_unblocker_config())

    import scraper.queue as q
    monkeypatch.setattr(q, "get_redis", lambda url: None)
    monkeypatch.setattr(w, "resolve_isp_pool", lambda conn: [])

    monkeypatch.setattr(w, "_scrape_one", lambda *a, **k: (_ for _ in ()).throw(Exception("x")))
    monkeypatch.setattr(w, "_bulk_upsert", lambda *a, **k: None)
    called = {"requeued": False}
    monkeypatch.setattr(w, "_requeue_failed", lambda *a, **k: called.update(requeued=True))
    w.scrape_batch(["https://www.ebay.com.au/itm/1"], "watch", "https://store", attempt=3)
    assert called["requeued"] is False


def test_warmed_session_warms_once_and_reuses(monkeypatch):
    import scraper.worker as w
    w._session_local.__dict__.pop("sessions", None)
    builds = []

    class _FakeSess:
        def __init__(self, proxy):
            self.proxy = proxy
            self.warmed = 0

        def get(self, url, **kwargs):
            self.warmed += 1
            return None

    monkeypatch.setattr(w, "build_session", lambda proxy=None: (builds.append(proxy) or _FakeSess(proxy)))

    s1 = w._warmed_session(None, "https://www.ebay.com.au/itm/111111111111")
    s2 = w._warmed_session(None, "https://www.ebay.com.au/itm/222222222222")
    assert s1 is s2          # same proxy+host reuses the warmed session
    assert len(builds) == 1  # session built once
    assert s1.warmed == 1    # homepage warmed exactly once

    s3 = w._warmed_session("http://proxy:8080", "https://www.ebay.com.au/itm/333333333333")
    assert s3 is not s1      # a different proxy gets its own warmed session
    assert len(builds) == 2


def test_escalates_to_unblocker_on_challenge(monkeypatch):
    import scraper.worker as worker
    from scraper.throttle import BoxProxyState, TokenBucket
    from scraper.unblocker import UnblockerConfig
    from scraper.fetch import ChallengeError

    def boom(*a, **k):
        raise ChallengeError("blocked")
    monkeypatch.setattr(worker, "_scrape_one", boom)
    monkeypatch.setattr(worker, "fetch_via_unblocker", lambda url, cfg, rc=None: "<html>item</html>")
    sentinel = object()
    monkeypatch.setattr(worker, "parse_item_html", lambda html, item_url: sentinel)
    cfg = UnblockerConfig(provider="oxylabs", username="u", password="p")
    out = worker._scrape_one_with_unblocker(
        "https://www.ebay.com.au/itm/1",
        None,
        BoxProxyState(0.15, 120),
        TokenBucket(100),
        cfg,
        None,
    )
    assert out is sentinel


def test_no_escalation_when_unblocker_off_reraises(monkeypatch):
    import scraper.worker as worker
    from scraper.throttle import BoxProxyState, TokenBucket
    from scraper.unblocker import UnblockerConfig
    from scraper.fetch import ChallengeError
    import pytest

    def boom(*a, **k):
        raise ChallengeError("blocked")
    monkeypatch.setattr(worker, "_scrape_one", boom)
    cfg = UnblockerConfig(provider="none", username=None, password=None)
    with pytest.raises(ChallengeError):
        worker._scrape_one_with_unblocker(
            "https://www.ebay.com/itm/1",
            None,
            BoxProxyState(0.15, 120),
            TokenBucket(100),
            cfg,
            None,
        )


def test_404_none_passes_through_no_escalation(monkeypatch):
    import scraper.worker as worker
    from scraper.throttle import BoxProxyState, TokenBucket
    from scraper.unblocker import UnblockerConfig

    monkeypatch.setattr(worker, "_scrape_one", lambda *a, **k: None)
    called = {"unblocker": False}

    def track(*a, **k):
        called["unblocker"] = True
        return "<html/>"
    monkeypatch.setattr(worker, "fetch_via_unblocker", track)
    cfg = UnblockerConfig(provider="oxylabs", username="u", password="p")
    out = worker._scrape_one_with_unblocker(
        "https://www.ebay.com/itm/1",
        None,
        BoxProxyState(0.15, 120),
        TokenBucket(100),
        cfg,
        None,
    )
    assert out is None
    assert called["unblocker"] is False


def test_run_pool_fetch_round_robin_and_split(monkeypatch):
    import scraper.worker as w
    pool = ["http://u:p@1.1.1.1:1", "http://u:p@2.2.2.2:1"]
    seen = []

    def fake_fetch(url, proxy):
        seen.append((url, proxy))
        if url.endswith("333333333333"):
            raise w.ChallengeError("blocked")
        return _pd(url.rsplit("/", 1)[1])

    urls = [
        "https://www.ebay.com/itm/111111111111",
        "https://www.ebay.com/itm/222222222222",
        "https://www.ebay.com/itm/333333333333",
        "https://www.ebay.com/itm/444444444444",
    ]
    results, failed = w._run_pool_fetch(
        urls, pool, max_rps_per_ip=1000.0, concurrency=8, fetch_one=fake_fetch
    )
    # round-robin: url[0]->pool[0], url[1]->pool[1], url[2]->pool[0], url[3]->pool[1]
    assignment = {u: p for u, p in seen}
    assert assignment[urls[0]] == pool[0]
    assert assignment[urls[1]] == pool[1]
    assert assignment[urls[2]] == pool[0]
    assert assignment[urls[3]] == pool[1]
    assert {r.item_id for r in results} == {"111111111111", "222222222222", "444444444444"}
    assert failed == ["https://www.ebay.com/itm/333333333333"]


def test_run_pool_fetch_none_result_is_not_failure(monkeypatch):
    import scraper.worker as w
    results, failed = w._run_pool_fetch(
        ["https://www.ebay.com/itm/111111111111"],
        ["http://u:p@1.1.1.1:1"],
        max_rps_per_ip=1000.0, concurrency=4,
        fetch_one=lambda url, proxy: None,
    )
    assert results == []
    assert failed == []


def test_scrape_batch_uses_isp_pool_when_present(monkeypatch):
    import scraper.worker as w

    class FakeSettings:
        database_url = "postgresql://x/y"
        redis_url = "redis://x"
        max_rps_per_ip = 1000.0
        worker_concurrency = 8
        challenge_escalation_threshold = 0.15
        challenge_cooldown_seconds = 1.0

    monkeypatch.setattr(w, "Settings", lambda: FakeSettings())
    monkeypatch.setattr(w, "_get_proxy_url", lambda s: "http://res")
    monkeypatch.setattr(w, "resolve_isp_pool", lambda conn: ["http://u:p@1.1.1.1:1", "http://u:p@2.2.2.2:1"])
    monkeypatch.setattr("scraper.queue.get_redis", lambda url: MagicMock())
    upserted = {}
    monkeypatch.setattr(w, "_bulk_upsert", lambda db, products, niche, store: upserted.update(n=len(products)))

    calls = []

    def fake_pool_fetch_one(url, proxy):
        calls.append((url, proxy))
        return _pd(url.rsplit("/", 1)[1])

    monkeypatch.setattr(w, "_pool_fetch_one", fake_pool_fetch_one)

    urls = [f"https://www.ebay.com/itm/{i}" * 1 for i in
            ("111111111111", "222222222222", "333333333333")]
    w.scrape_batch(urls, "watch", "https://www.ebay.com/str/s", 0)

    assert upserted["n"] == 3
    # all fetches used pool IPs, never the residential proxy
    assert all(p in ("http://u:p@1.1.1.1:1", "http://u:p@2.2.2.2:1") for _, p in calls)

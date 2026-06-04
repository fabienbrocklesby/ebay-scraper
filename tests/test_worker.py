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


def test_scrape_batch_fetches_concurrently_and_bulk_writes(monkeypatch):
    import scraper.worker as w
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("DATABASE_URL", "postgresql://x/y")
    monkeypatch.setattr(w, "_get_proxy_url", lambda s: None)

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
    monkeypatch.setattr(w, "_scrape_one", lambda *a, **k: (_ for _ in ()).throw(Exception("x")))
    monkeypatch.setattr(w, "_bulk_upsert", lambda *a, **k: None)
    called = {"requeued": False}
    monkeypatch.setattr(w, "_requeue_failed", lambda *a, **k: called.update(requeued=True))
    w.scrape_batch(["https://www.ebay.com.au/itm/1"], "watch", "https://store", attempt=3)
    assert called["requeued"] is False

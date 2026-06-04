import pytest
from unittest.mock import MagicMock, patch
from scraper.scraper import ProductData


def make_mock_product():
    return ProductData(
        item_id="123",
        title="Test Product",
        price=9.99,
        currency="USD",
        condition="New",
        description="",
        image_urls="https://img.ebay.com/1.jpg",
        item_url="https://www.ebay.com/itm/123",
        seller_id="seller1",
        category="Electronics",
        item_specifics="{}",
        mpn="",
        upc="",
        shipping="Free",
        listing_type="Buy It Now",
    )


ITEM_URL = "https://www.ebay.com/itm/123"


def test_scrape_and_store_inserts_on_success(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("DATABASE_URL", "postgresql://scraper:scraper@localhost/ebayscraper")
    monkeypatch.setenv("PROXY_URL", "")

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_conn.__enter__ = lambda s: s
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_cursor.__enter__ = lambda s: s
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value = mock_cursor

    with patch("scraper.worker.scrape_item", return_value=make_mock_product()), \
         patch("scraper.worker.psycopg2.connect", return_value=mock_conn):
        from scraper.worker import scrape_and_store
        scrape_and_store(ITEM_URL, "electronics", "https://www.ebay.com/str/test")
        assert mock_cursor.execute.called
        assert mock_conn.commit.called


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


def test_scrape_and_store_skips_on_none_result(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("DATABASE_URL", "postgresql://scraper:scraper@localhost/ebayscraper")
    monkeypatch.setenv("PROXY_URL", "")

    mock_conn = MagicMock()

    with patch("scraper.worker.scrape_item", return_value=None), \
         patch("scraper.worker.psycopg2.connect", return_value=mock_conn):
        from scraper.worker import scrape_and_store
        scrape_and_store("https://www.ebay.com/itm/000", "electronics", "https://www.ebay.com/str/test")
        assert not mock_conn.commit.called


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

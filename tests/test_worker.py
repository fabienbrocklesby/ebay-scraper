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

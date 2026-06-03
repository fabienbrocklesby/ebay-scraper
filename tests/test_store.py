import pytest
import respx
import httpx
from scraper.store import extract_seller_id, _normalize_store_url, _extract_item_urls, get_item_urls_from_store


def test_extract_seller_id_from_str_url():
    assert extract_seller_id("https://www.ebay.com/str/coolstore") == "coolstore"


def test_extract_seller_id_from_str_url_trailing_slash():
    assert extract_seller_id("https://www.ebay.com/str/coolstore/") == "coolstore"


def test_extract_seller_id_from_sch_url():
    assert extract_seller_id("https://www.ebay.com/sch/username/m.html") == "username"


def test_normalize_store_url_strips_trailing_slash():
    assert _normalize_store_url("https://www.ebay.com/str/coolstore/") == "https://www.ebay.com/str/coolstore"


def test_normalize_store_url_strips_query_params():
    assert _normalize_store_url("https://www.ebay.com/str/coolstore?_trksid=abc") == "https://www.ebay.com/str/coolstore"


def test_extract_item_urls_parses_str_item_card_links():
    html = """
    <html><body>
      <a class="str-item-card__link" href="https://www.ebay.com/itm/111111111?itmmeta=abc">Item 1</a>
      <a class="str-item-card__link" href="https://www.ebay.com/itm/222222222?itmmeta=def">Item 2</a>
    </body></html>
    """
    urls = _extract_item_urls(html)
    assert "https://www.ebay.com/itm/111111111" in urls
    assert "https://www.ebay.com/itm/222222222" in urls
    assert len(urls) == 2


def test_extract_item_urls_handles_au_domain():
    html = """
    <html><body>
      <a class="str-item-card__link" href="https://www.ebay.com.au/itm/399000000001?itmmeta=x">AU Item</a>
    </body></html>
    """
    urls = _extract_item_urls(html)
    assert "https://www.ebay.com.au/itm/399000000001" in urls


def test_extract_item_urls_deduplicates():
    html = """
    <html><body>
      <a class="str-item-card__link" href="https://www.ebay.com/itm/111?itmmeta=a">Item</a>
      <a class="str-item-card__link" href="https://www.ebay.com/itm/111?itmmeta=b">Item again</a>
    </body></html>
    """
    urls = _extract_item_urls(html)
    assert urls.count("https://www.ebay.com/itm/111") == 1


@respx.mock
def test_get_item_urls_from_store_returns_urls():
    homepage_html = "<html><body>eBay homepage</body></html>"
    store_html = """
    <html><body>
      <a class="str-item-card__link" href="https://www.ebay.com/itm/111111111?itmmeta=abc">Item 1</a>
      <a class="str-item-card__link" href="https://www.ebay.com/itm/222222222?itmmeta=def">Item 2</a>
    </body></html>
    """
    respx.get("https://www.ebay.com/").mock(return_value=httpx.Response(200, text=homepage_html))
    respx.get("https://www.ebay.com/str/testseller").mock(return_value=httpx.Response(200, text=store_html))

    session = httpx.Client(follow_redirects=True)
    urls = get_item_urls_from_store(
        "https://www.ebay.com/str/testseller", max_pages=1, _session=session
    )
    assert "https://www.ebay.com/itm/111111111" in urls
    assert "https://www.ebay.com/itm/222222222" in urls
    assert len(urls) == 2


@respx.mock
def test_get_item_urls_returns_empty_on_no_listings():
    respx.get("https://www.ebay.com/").mock(return_value=httpx.Response(200, text="<html></html>"))
    respx.get("https://www.ebay.com/str/emptyseller").mock(return_value=httpx.Response(200, text="<html><body></body></html>"))

    session = httpx.Client(follow_redirects=True)
    urls = get_item_urls_from_store(
        "https://www.ebay.com/str/emptyseller", max_pages=1, _session=session
    )
    assert urls == []

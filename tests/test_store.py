import pytest
import respx
import httpx
from scraper.store import extract_seller_id, build_store_search_url, get_item_ids_from_store


def test_extract_seller_id_from_str_url():
    assert extract_seller_id("https://www.ebay.com/str/coolstore") == "coolstore"


def test_extract_seller_id_from_str_url_trailing_slash():
    assert extract_seller_id("https://www.ebay.com/str/coolstore/") == "coolstore"


def test_extract_seller_id_from_sch_url():
    assert extract_seller_id("https://www.ebay.com/sch/username/m.html") == "username"


def test_build_store_search_url_contains_seller_and_page():
    url = build_store_search_url("myseller", page=3)
    assert "myseller" in url
    assert "_pgn=3" in url
    assert "_ipg=240" in url


@respx.mock
def test_get_item_ids_from_store_single_page():
    html = """
    <html><body>
    <ul class="srp-results">
      <li class="s-item">
        <a class="s-item__link" href="https://www.ebay.com/itm/111111111">Item 1</a>
      </li>
      <li class="s-item">
        <a class="s-item__link" href="https://www.ebay.com/itm/222222222">Item 2</a>
      </li>
    </ul>
    </body></html>
    """
    respx.get("https://www.ebay.com/sch/i.html").mock(
        return_value=httpx.Response(200, text=html)
    )
    ids = get_item_ids_from_store("https://www.ebay.com/str/testseller", max_pages=1)
    assert "111111111" in ids
    assert "222222222" in ids
    assert len(ids) == 2


@respx.mock
def test_get_item_ids_returns_empty_on_no_listings():
    html = "<html><body><ul class='srp-results'></ul></body></html>"
    respx.get("https://www.ebay.com/sch/i.html").mock(
        return_value=httpx.Response(200, text=html)
    )
    ids = get_item_ids_from_store("https://www.ebay.com/str/emptyseller", max_pages=1)
    assert ids == []


@respx.mock
def test_get_item_ids_deduplicates():
    html = """
    <html><body>
    <ul class="srp-results">
      <li class="s-item">
        <a class="s-item__link" href="https://www.ebay.com/itm/111">Item 1</a>
      </li>
      <li class="s-item">
        <a class="s-item__link" href="https://www.ebay.com/itm/111">Item 1 again</a>
      </li>
    </ul>
    </body></html>
    """
    respx.get("https://www.ebay.com/sch/i.html").mock(
        return_value=httpx.Response(200, text=html)
    )
    ids = get_item_ids_from_store("https://www.ebay.com/str/dupe", max_pages=1)
    assert ids.count("111") == 1

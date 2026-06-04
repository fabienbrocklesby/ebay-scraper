import pytest
import respx
import httpx
from scraper.fetch import ChallengeError
from scraper.store import extract_seller_id, _normalize_store_url, _extract_item_urls, get_item_urls_from_store
from scraper.store import _price_partitions


def test_price_partitions_splits_range():
    parts = _price_partitions(0, 1000, splits=4)
    assert parts == [(0, 250), (250, 500), (500, 750), (750, 1000)]


def test_price_partition_url_params():
    from scraper.store import _partition_url
    u = _partition_url("https://www.ebay.com.au/str/x", 250, 500)
    assert "_udlo=250" in u and "_udhi=500" in u


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


@respx.mock
def test_get_item_urls_raises_on_challenge_not_silent_truncation():
    """A bot-challenge page (HTTP 200) must not be read as the end of the store."""
    challenge_html = "<html><head><title>Security Measure | eBay</title></head></html>"
    respx.get("https://www.ebay.com/").mock(return_value=httpx.Response(200, text="<html></html>"))
    respx.get("https://www.ebay.com/str/blockedseller").mock(
        return_value=httpx.Response(200, text=challenge_html)
    )
    session = httpx.Client(follow_redirects=True)
    with pytest.raises(ChallengeError):
        get_item_urls_from_store(
            "https://www.ebay.com/str/blockedseller", max_pages=1, _session=session
        )


class _FakeResponse:
    def __init__(self, text: str):
        self.status_code = 200
        self.text = text


class _FakeSession:
    """Serves one canned page for any request, simulating one proxy exit IP."""

    def __init__(self, text: str):
        self._text = text

    def get(self, url: str, **kwargs) -> _FakeResponse:
        return _FakeResponse(self._text)

    def close(self) -> None:
        pass


def test_recovers_from_transient_challenge(monkeypatch):
    """A challenge mid-crawl must be retried on a fresh session, not silently dropped."""
    challenge = "<html><head><title>Security Measure | eBay</title></head></html>"
    good = (
        '<html><body>'
        '<a class="str-item-card__link" href="https://www.ebay.com/itm/555">x</a>'
        '</body></html>'
    )
    # Initial session challenges; first retry still blocked; second retry succeeds.
    behaviours = [challenge, challenge, good]
    monkeypatch.setattr("scraper.store.build_session", lambda proxy_url=None: _FakeSession(behaviours.pop(0)))
    monkeypatch.setattr("scraper.store.time.sleep", lambda _seconds: None)

    urls = get_item_urls_from_store("https://www.ebay.com/str/seller", max_pages=1)

    assert urls == ["https://www.ebay.com/itm/555"]
    assert behaviours == []

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


def test_extract_seller_id_from_sch_ssn_url():
    # /sch/i.html?_ssn=seller is the common format when stores are added via seller search.
    # The path segment after "sch" is always "i.html", so the seller name is in _ssn.
    assert extract_seller_id("https://www.ebay.com/sch/i.html?_ssn=rokyautoparts") == "rokyautoparts"


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


def test_normalize_sch_url_keeps_seller_search():
    from scraper.store import _normalize_store_url
    url = "https://www.ebay.com/sch/i.html?_ssn=redtiger_store&store_name=vlrd5820&_oac=1"
    assert _normalize_store_url(url) == "https://www.ebay.com/sch/i.html?_ssn=redtiger_store"


def test_page_url_appends_to_existing_query():
    from scraper.store import _page_url
    assert _page_url("https://www.ebay.com/str/x", 1) == "https://www.ebay.com/str/x?_pgn=1&_ipg=240"
    sch = _page_url("https://www.ebay.com/sch/i.html?_ssn=redtiger_store", 2)
    assert sch == "https://www.ebay.com/sch/i.html?_ssn=redtiger_store&_pgn=2&_ipg=240"


def test_extract_item_urls_general_fallback_for_search_pages():
    from scraper.store import _extract_item_urls
    html = ('<div><a href="https://www.ebay.com/itm/123456789012?hash=z">a</a>'
            '<a href="https://www.ebay.com/itm/987654321098">b</a></div>')
    urls = _extract_item_urls(html)
    assert "https://www.ebay.com/itm/123456789012" in urls
    assert "https://www.ebay.com/itm/987654321098" in urls


def _unblocker_page(item_ids, has_next):
    items = "".join(
        f'<a class="str-item-card__link" href="https://www.ebay.com/itm/{i}">x</a>'
        for i in item_ids
    )
    nxt = '<a class="pagination__next" href="#">Next</a>' if has_next else ""
    return f"<html><body>{items}{nxt}</body></html>"


def _page_num(url):
    import re as _re
    return int(_re.search(r"_pgn=(\d+)", url).group(1))


def test_get_item_urls_via_unblocker_paginates_until_no_next():
    from scraper.store import get_item_urls_via_unblocker
    pages = {
        1: _unblocker_page(["111111111111", "222222222222"], has_next=True),
        2: _unblocker_page(["333333333333"], has_next=False),
    }
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return pages.get(_page_num(url))

    urls = get_item_urls_via_unblocker("https://www.ebay.com/sch/i.html?_ssn=x", fake_fetch)
    assert urls == [
        "https://www.ebay.com/itm/111111111111",
        "https://www.ebay.com/itm/222222222222",
        "https://www.ebay.com/itm/333333333333",
    ]
    assert [_page_num(c) for c in calls] == [1, 2]


def test_get_item_urls_via_unblocker_stops_on_empty_page():
    from scraper.store import get_item_urls_via_unblocker
    pages = {1: _unblocker_page(["111111111111"], has_next=True), 2: "<html></html>"}
    urls = get_item_urls_via_unblocker(
        "https://www.ebay.com/sch/i.html?_ssn=x", lambda u: pages.get(_page_num(u))
    )
    assert urls == ["https://www.ebay.com/itm/111111111111"]


def test_get_item_urls_via_unblocker_empty_when_fetch_fails():
    from scraper.store import get_item_urls_via_unblocker
    urls = get_item_urls_via_unblocker(
        "https://www.ebay.com/sch/i.html?_ssn=x", lambda u: None
    )
    assert urls == []


def test_get_item_urls_via_unblocker_treats_challenge_as_stop_not_data():
    from scraper.store import get_item_urls_via_unblocker
    pages = {
        1: _unblocker_page(["111111111111"], has_next=True),
        2: "<html><title>Security Measure</title></html>",
    }
    urls = get_item_urls_via_unblocker(
        "https://www.ebay.com/sch/i.html?_ssn=x", lambda u: pages.get(_page_num(u))
    )
    assert urls == ["https://www.ebay.com/itm/111111111111"]


def test_get_item_urls_retries_flaky_empty_page_not_truncate(monkeypatch):
    """A transient empty page mid-crawl must be retried on a fresh session, not
    read as end-of-store. In production a 0-item page wedged between full pages
    truncated a 1.9M store to ~1,400 items."""
    import re as _re

    def page(ids, nxt):
        items = "".join(
            f'<a class="str-item-card__link" href="https://www.ebay.com/itm/{i}">x</a>'
            for i in ids
        )
        n = '<a class="pagination__next" href="#">Next</a>' if nxt else ""
        return f"<html><body>{items}{n}</body></html>"

    class Scripted:
        def __init__(self, pages):
            self.pages = pages

        def get(self, url, **kw):
            m = _re.search(r"_pgn=(\d+)", url)
            html = "<html><body>home</body></html>" if not m else self.pages.get(
                int(m.group(1)), "<html></html>"
            )
            return _FakeResponse(html)

        def close(self):
            pass

    sessions = [
        Scripted({1: page(["111111111111"], True), 2: "<html><body></body></html>"}),
        Scripted({2: page(["222222222222"], False)}),
    ]
    monkeypatch.setattr("scraper.store.build_session", lambda proxy_url=None: sessions.pop(0))
    monkeypatch.setattr("scraper.store.time.sleep", lambda *_a: None)

    urls = get_item_urls_from_store("https://www.ebay.com/str/seller", proxy_url="http://x")
    assert "https://www.ebay.com/itm/111111111111" in urls
    assert "https://www.ebay.com/itm/222222222222" in urls
    assert sessions == []  # initial session + one fresh-IP retry, both consumed


def test_get_item_urls_genuinely_empty_store_ends_after_retries(monkeypatch):
    """A store that is empty on every retry ends cleanly with no items."""
    class Empty:
        def get(self, url, **kw):
            return _FakeResponse("<html><body></body></html>")

        def close(self):
            pass

    monkeypatch.setattr("scraper.store.build_session", lambda proxy_url=None: Empty())
    monkeypatch.setattr("scraper.store.time.sleep", lambda *_a: None)
    urls = get_item_urls_from_store("https://www.ebay.com/str/seller", proxy_url="http://x")
    assert urls == []

from scraper.marketplace import (
    detect_marketplace,
    MarketplaceResult,
    DetectionOutcome,
    CANDIDATE_MARKETPLACES,
)
from scraper.fetch import ChallengeError


def _grid_html(n: int) -> str:
    cards = "".join(
        f'<a class="str-item-card__link" href="https://x/itm/{1000+i}?x">i</a>'
        for i in range(n)
    )
    return f"<html><body>{cards}</body></html>"


def test_picks_domain_with_most_items():
    def fetch_fn(url, proxy_url):
        if "ebay.com.au" in url:
            return _grid_html(240)
        if "ebay.co.uk" in url:
            return _grid_html(5)
        return _grid_html(0)  # ebay.com genuine zero

    outcome = detect_marketplace("seller1", proxy_url=None, fetch_fn=fetch_fn)
    assert isinstance(outcome, DetectionOutcome)
    assert outcome.result is not None
    assert outcome.result.domain == "www.ebay.com.au"
    assert outcome.result.country == "au"
    assert outcome.result.item_count == 240


def test_challenge_is_retried_not_counted_as_zero():
    calls = {"n": 0}

    def fetch_fn(url, proxy_url):
        if "ebay.com.au" in url:
            calls["n"] += 1
            if calls["n"] == 1:
                raise ChallengeError("blocked once")
            return _grid_html(120)
        return _grid_html(0)

    outcome = detect_marketplace("seller2", proxy_url=None, fetch_fn=fetch_fn)
    assert outcome.result is not None
    assert outcome.result.domain == "www.ebay.com.au"
    assert outcome.result.item_count == 120


def test_all_genuine_zero_returns_no_result():
    outcome = detect_marketplace(
        "seller3", proxy_url=None, fetch_fn=lambda url, proxy_url: _grid_html(0)
    )
    assert outcome.result is None
    assert outcome.undetermined_domains == []


def test_persistent_challenge_is_undetermined():
    def fetch_fn(url, proxy_url):
        raise ChallengeError("always blocked")

    outcome = detect_marketplace("seller4", proxy_url=None, fetch_fn=fetch_fn)
    assert outcome.result is None
    assert set(outcome.undetermined_domains) == {d for d, _ in CANDIDATE_MARKETPLACES}


def test_challenge_page_html_is_retried_not_counted_as_zero():
    calls = {"n": 0}

    def fetch_fn(url, proxy_url):
        if "ebay.com.au" in url:
            calls["n"] += 1
            if calls["n"] == 1:
                return "<html><head><title>Security Measure | eBay</title></head></html>"
            return _grid_html(80)
        return _grid_html(0)

    outcome = detect_marketplace("seller5", proxy_url=None, fetch_fn=fetch_fn)
    assert outcome.result is not None
    assert outcome.result.domain == "www.ebay.com.au"
    assert outcome.result.item_count == 80

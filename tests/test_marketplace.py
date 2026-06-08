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


def test_probes_storefront_not_seller_search():
    seen = []

    def fetch_fn(url, proxy_url):
        seen.append(url)
        return _grid_html(10) if "ebay.com.au" in url else _grid_html(0)

    outcome = detect_marketplace("mystore", proxy_url=None, fetch_fn=fetch_fn)
    assert seen, "detection should probe at least one domain"
    assert all("/str/mystore" in u for u in seen)
    assert all("/sch/" not in u for u in seen)
    assert outcome.result.seller_search_url.startswith("https://www.ebay.com.au/str/mystore")


def test_cross_listing_tie_breaks_to_priority_domain():
    # Cross-border listings cap at a full page on several domains, so a tie is normal.
    # It must break toward the earlier (higher-priority) candidate, not be abandoned.
    def fetch_fn(url, proxy_url):
        if "ebay.de" in url:
            return _grid_html(200)
        if "ebay.com.au" in url:
            return _grid_html(200)
        return _grid_html(2)

    outcome = detect_marketplace("tie", proxy_url=None, fetch_fn=fetch_fn)
    assert outcome.result is not None
    assert outcome.result.country == "au"  # .com.au precedes .de in candidate order


def test_clear_winner_with_small_cross_listings_still_resolves():
    # A handful of cross-listings on another domain must not block a confident home.
    def fetch_fn(url, proxy_url):
        if "ebay.com.au" in url:
            return _grid_html(200)
        if "ebay.com" in url and "com.au" not in url:
            return _grid_html(8)
        return _grid_html(0)

    outcome = detect_marketplace("clearau", proxy_url=None, fetch_fn=fetch_fn)
    assert outcome.result is not None
    assert outcome.result.country == "au"


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

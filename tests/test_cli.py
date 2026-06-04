import click
import pytest

from scraper.cli import _parse_store_lines


def test_parse_store_lines_mixed_niches():
    text = """
# comment line ignored
https://www.ebay.com.au/str/storeone, watches

https://www.ebay.com.au/str/storetwo
https://www.ebay.com/str/storethree,car-parts
"""
    out = _parse_store_lines(text, default_niche="default")
    assert len(out) == 3
    assert out[0][1] == "watches"      # line's own niche wins
    assert out[1][1] == "default"      # falls back to --niche default
    assert out[2][1] == "car-parts"
    assert all(u.startswith("http") for u, _ in out)


def test_parse_store_lines_requires_a_niche():
    with pytest.raises(click.ClickException):
        _parse_store_lines("https://www.ebay.com.au/str/x", default_niche=None)


def test_discover_store_proxy_first_then_direct_fallback(monkeypatch):
    import scraper.cli as c
    calls = []

    def fake(url, proxy_url=None, requests_per_second=0.5):
        calls.append(proxy_url)
        if proxy_url is not None:
            return []   # proxy served the degraded "0 results" view
        return ["https://www.ebay.com/itm/123456789012"]   # genuine IP gets the grid

    monkeypatch.setattr(c, "get_item_urls_from_store", fake)
    outcome, urls = c._discover_store("https://www.ebay.com/str/x", "http://proxy", 0.5)
    assert outcome == "ok" and len(urls) == 1
    assert calls == ["http://proxy", None]   # proxy first, then direct fallback


def test_discover_store_empty_trusts_clean_zero(monkeypatch):
    import scraper.cli as c
    monkeypatch.setattr(c, "get_item_urls_from_store",
                        lambda url, proxy_url=None, requests_per_second=0.5: [])
    # clean IP returns 0 -> genuinely empty, do not burn a proxy attempt
    assert c._discover_store("https://www.ebay.com/str/x", "http://proxy", 0.5) == ("empty", [])


def test_discover_store_blocked_when_both_challenge(monkeypatch):
    import scraper.cli as c
    from scraper.fetch import ChallengeError

    def fake(url, proxy_url=None, requests_per_second=0.5):
        raise ChallengeError("blocked")

    monkeypatch.setattr(c, "get_item_urls_from_store", fake)
    assert c._discover_store("https://www.ebay.com/str/x", "http://proxy", 0.5)[0] == "blocked"

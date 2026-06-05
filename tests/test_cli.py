import click
import pytest

from scraper.cli import _parse_store_lines


def test_parse_store_lines_no_niche_defaults_empty():
    text = "# header\nhttps://www.ebay.com/str/one\n\nhttps://www.ebay.com.au/str/two\n"
    out = _parse_store_lines(text, default_niche="")
    assert out == [
        ("https://www.ebay.com/str/one", ""),
        ("https://www.ebay.com.au/str/two", ""),
    ]


def test_run_command_registered():
    from click.testing import CliRunner
    from scraper.cli import cli
    result = CliRunner().invoke(cli, ["run", "--help"])
    assert result.exit_code == 0
    assert "rows-per-file" in result.output


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


def test_resolve_marketplace_url_uses_cached_domain():
    from scraper.cli import _resolve_marketplace_url
    url = _resolve_marketplace_url(
        store_url="https://www.ebay.com/str/aussie",
        cached=("www.ebay.com.au", "au"),
        detect=lambda seller_id, proxy_url: (_ for _ in ()).throw(AssertionError("should not detect")),
        proxy_url=None,
    )
    assert url == "https://www.ebay.com.au/sch/i.html?_ssn=aussie"


def test_resolve_marketplace_url_detects_when_uncached():
    from scraper.cli import _resolve_marketplace_url
    from scraper.marketplace import DetectionOutcome, MarketplaceResult
    captured = {}

    def fake_detect(seller_id, proxy_url):
        return DetectionOutcome(
            result=MarketplaceResult("www.ebay.com.au", "au", 200,
                                     "https://www.ebay.com.au/sch/i.html?_ssn=aussie&_pgn=1&_ipg=60"),
            undetermined_domains=[],
        )

    url = _resolve_marketplace_url(
        store_url="https://www.ebay.com/str/aussie",
        cached=None,
        detect=fake_detect,
        proxy_url=None,
        on_detected=lambda domain, country: captured.update(domain=domain, country=country),
    )
    assert url == "https://www.ebay.com.au/sch/i.html?_ssn=aussie"
    assert captured == {"domain": "www.ebay.com.au", "country": "au"}


def test_discover_store_empty_trusts_clean_zero(monkeypatch):
    import scraper.cli as c
    monkeypatch.setattr(c, "get_item_urls_from_store",
                        lambda url, proxy_url=None, requests_per_second=0.5: [])
    # cached marketplace skips detection; a clean crawl with no items is a genuinely empty store
    assert c._discover_store(
        "https://www.ebay.com/str/x", "http://proxy", 0.5, cached=("www.ebay.com", "us")
    ) == ("empty", [])


def test_discover_store_blocked_on_crawl_challenge(monkeypatch):
    import scraper.cli as c
    from scraper.fetch import ChallengeError

    def fake(url, proxy_url=None, requests_per_second=0.5):
        raise ChallengeError("blocked")

    monkeypatch.setattr(c, "get_item_urls_from_store", fake)
    assert c._discover_store(
        "https://www.ebay.com/str/x", "http://proxy", 0.5, cached=("www.ebay.com", "us")
    )[0] == "blocked"


def test_setup_command_registered():
    from click.testing import CliRunner
    from scraper.cli import cli
    result = CliRunner().invoke(cli, ["setup", "--help"])
    assert result.exit_code == 0


def test_probe_proxy_ok_is_callable(monkeypatch):
    from scraper import cli as climod
    monkeypatch.setattr(climod, "_probe_proxy_ok", lambda proxy_url: True, raising=False)
    assert climod._probe_proxy_ok("http://x") is True

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


def test_detect_and_resolve_adopts_confident_proxy_result(monkeypatch):
    import scraper.cli as c
    from scraper.marketplace import DetectionOutcome, MarketplaceResult
    captured = {}

    monkeypatch.setattr(c, "detect_marketplace", lambda seller_id, proxy_url, **kw: DetectionOutcome(
        result=MarketplaceResult("www.ebay.com.au", "au", 240,
                                 "https://www.ebay.com.au/sch/i.html?_ssn=aussie&_pgn=1&_ipg=240"),
        undetermined_domains=[],
    ))

    url = c._detect_and_resolve(
        seller_id="aussie",
        proxy_url="http://proxy",
        on_detected=lambda domain, country: captured.update(domain=domain, country=country),
        unblocker_config=None,
        redis_conn=None,
    )
    assert url == "https://www.ebay.com.au/sch/i.html?_ssn=aussie"
    assert captured == {"domain": "www.ebay.com.au", "country": "au"}


def test_detect_and_resolve_does_not_adopt_when_undetermined_and_unblocker_off(monkeypatch):
    import scraper.cli as c
    from scraper.marketplace import DetectionOutcome, MarketplaceResult
    from scraper.unblocker import UnblockerConfig
    called = {"on_detected": False}

    monkeypatch.setattr(c, "detect_marketplace", lambda seller_id, proxy_url, **kw: DetectionOutcome(
        result=MarketplaceResult("www.ebay.com.au", "au", 186,
                                 "https://www.ebay.com.au/sch/i.html?_ssn=aussie&_pgn=1&_ipg=240"),
        undetermined_domains=["www.ebay.com"],
    ))

    url = c._detect_and_resolve(
        seller_id="aussie",
        proxy_url="http://proxy",
        on_detected=lambda domain, country: called.update(on_detected=True),
        unblocker_config=UnblockerConfig("none", None, None),
        redis_conn=None,
    )
    assert url is None
    assert called["on_detected"] is False


def test_detect_and_resolve_escalates_to_unblocker_when_undetermined(monkeypatch):
    import scraper.cli as c
    from scraper.marketplace import DetectionOutcome, MarketplaceResult
    from scraper.unblocker import UnblockerConfig
    captured = {}

    def fake_detect(seller_id, proxy_url, **kwargs):
        if "fetch_fn" in kwargs:  # authoritative unblocker probe
            return DetectionOutcome(
                result=MarketplaceResult("www.ebay.com", "us", 312,
                                         "https://www.ebay.com/sch/i.html?_ssn=aussie&_pgn=1&_ipg=240"),
                undetermined_domains=[],
            )
        return DetectionOutcome(  # proxy probe: home left undetermined
            result=MarketplaceResult("www.ebay.com.au", "au", 186,
                                     "https://www.ebay.com.au/sch/i.html?_ssn=aussie&_pgn=1&_ipg=240"),
            undetermined_domains=["www.ebay.com"],
        )

    monkeypatch.setattr(c, "detect_marketplace", fake_detect)
    monkeypatch.setattr(c, "fetch_via_unblocker", lambda url, cfg, conn: "<html/>")

    url = c._detect_and_resolve(
        seller_id="aussie",
        proxy_url="http://proxy",
        on_detected=lambda domain, country: captured.update(domain=domain, country=country),
        unblocker_config=UnblockerConfig("oxylabs", "user", "pass"),
        redis_conn=None,
    )
    assert url == "https://www.ebay.com/sch/i.html?_ssn=aussie"
    assert captured == {"domain": "www.ebay.com", "country": "us"}


def test_discover_store_cached_skips_detection(monkeypatch):
    import scraper.cli as c
    monkeypatch.setattr(c, "detect_marketplace",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not detect")))
    monkeypatch.setattr(c, "get_item_urls_from_store",
                        lambda url, proxy_url=None, requests_per_second=0.5: [])
    assert c._discover_store(
        "https://www.ebay.com/str/aussie", "http://proxy", 0.5, cached=("www.ebay.com.au", "au")
    ) == ("empty", [])


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


def test_doctor_command_registered():
    from click.testing import CliRunner
    from scraper.cli import cli
    result = CliRunner().invoke(cli, ["doctor", "--help"])
    assert result.exit_code == 0


def test_redact_url_masks_password():
    from scraper.cli import _redact_url
    assert _redact_url("postgresql://scraper:secret@host/db") == "postgresql://scraper:***@host/db"
    assert _redact_url("redis://host:6379") == "redis://host:6379"  # nothing to redact

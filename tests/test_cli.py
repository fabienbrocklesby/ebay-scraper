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
    # Detection resolves to the seller's home-marketplace storefront (/str), which is
    # what the crawl can actually paginate (eBay challenges /sch from proxies).
    assert url == "https://www.ebay.com.au/str/aussie"
    assert captured == {"domain": "www.ebay.com.au", "country": "au"}


def test_detect_and_resolve_adopts_best_even_when_some_domains_undetermined(monkeypatch):
    import scraper.cli as c
    from scraper.marketplace import DetectionOutcome, MarketplaceResult
    from scraper.unblocker import UnblockerConfig
    captured = {}

    # A clear winner (.com.au) with another candidate left undetermined is still
    # adopted: cross-listing means an undetermined sibling is not a reason to abandon
    # a conclusive home marketplace.
    monkeypatch.setattr(c, "detect_marketplace", lambda seller_id, proxy_url, **kw: DetectionOutcome(
        result=MarketplaceResult("www.ebay.com.au", "au", 186,
                                 "https://www.ebay.com.au/sch/i.html?_ssn=aussie&_pgn=1&_ipg=240"),
        undetermined_domains=["www.ebay.com"],
    ))

    url = c._detect_and_resolve(
        seller_id="aussie",
        proxy_url="http://proxy",
        on_detected=lambda domain, country: captured.update(domain=domain, country=country),
        unblocker_config=UnblockerConfig("none", None, None),
        redis_conn=None,
    )
    assert url == "https://www.ebay.com.au/str/aussie"
    assert captured == {"domain": "www.ebay.com.au", "country": "au"}


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
        # Proxy probe is challenged on every candidate, so it yields no conclusive
        # home marketplace: only then does detection escalate to the unblocker.
        return DetectionOutcome(
            result=None,
            undetermined_domains=["www.ebay.com", "www.ebay.com.au"],
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
    assert url == "https://www.ebay.com/str/aussie"
    assert captured == {"domain": "www.ebay.com", "country": "us"}


def test_discover_store_cached_skips_detection(monkeypatch):
    import scraper.cli as c
    monkeypatch.setattr(c, "detect_marketplace",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not detect")))
    monkeypatch.setattr(c, "get_item_urls_from_store",
                        lambda url, proxy_url=None, requests_per_second=0.5, max_pages=9999, max_challenge_retries=4: [])
    assert c._discover_store(
        "https://www.ebay.com/str/aussie", "http://proxy", 0.5, cached=("www.ebay.com.au", "au")
    ) == ("empty", [])


def test_discover_store_empty_trusts_clean_zero(monkeypatch):
    import scraper.cli as c
    monkeypatch.setattr(c, "get_item_urls_from_store",
                        lambda url, proxy_url=None, requests_per_second=0.5, max_pages=9999, max_challenge_retries=4: [])
    # cached marketplace skips detection; a clean crawl with no items is a genuinely empty store
    assert c._discover_store(
        "https://www.ebay.com/str/x", "http://proxy", 0.5, cached=("www.ebay.com", "us")
    ) == ("empty", [])


def test_discover_store_blocked_on_crawl_challenge(monkeypatch):
    import scraper.cli as c
    from scraper.fetch import ChallengeError

    def fake(url, proxy_url=None, requests_per_second=0.5, max_pages=9999, max_challenge_retries=4):
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


def test_wait_for_drain_aborts_without_workers(monkeypatch):
    import types
    import scraper.cli as cli
    import click
    import pytest
    monkeypatch.setattr(cli, "queue_is_drained", lambda q: False)
    monkeypatch.setattr(cli, "Worker", type("W", (), {"all": staticmethod(lambda queue=None: [])}))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    with pytest.raises(click.ClickException):
        cli._wait_for_drain(types.SimpleNamespace(count=0))


def test_probe_proxy_ok_true_if_any_marketplace_clean(monkeypatch):
    from scraper import cli
    monkeypatch.setattr(cli, "_probe_proxy_markets", lambda p: {"US": False, "AU": True, "UK": False})
    assert cli._probe_proxy_ok("http://x") is True


def test_probe_proxy_ok_false_if_all_marketplaces_flagged(monkeypatch):
    from scraper import cli
    monkeypatch.setattr(cli, "_probe_proxy_markets", lambda p: {"US": False, "AU": False, "UK": False})
    assert cli._probe_proxy_ok("http://x") is False


class _FakePoolRedis:
    def __init__(self):
        self.members = set()
    def sadd(self, key, val):
        before = len(self.members)
        self.members.add(val)
        return len(self.members) - before
    def srem(self, key, val):
        if val in self.members:
            self.members.discard(val)
            return 1
        return 0
    def smembers(self, key):
        return set(self.members)
    def delete(self, key):
        self.members.clear()


def _pool_runner(monkeypatch):
    from click.testing import CliRunner
    import scraper.cli as c
    fake = _FakePoolRedis()
    monkeypatch.setattr(c, "get_redis", lambda url: fake)
    monkeypatch.setattr(c, "Settings", lambda: type("S", (), {"redis_url": "redis://x"})())
    return CliRunner(), fake


def test_proxy_pool_add_normalizes_and_stores(monkeypatch):
    from scraper.cli import cli
    runner, fake = _pool_runner(monkeypatch)
    result = runner.invoke(cli, ["proxy", "pool", "add", "1.2.3.4:9999:user:pass"])
    assert result.exit_code == 0
    assert fake.members == {"http://user:pass@1.2.3.4:9999"}
    assert "1 IP(s)" in result.output


def test_proxy_pool_add_rejects_garbage(monkeypatch):
    from scraper.cli import cli
    runner, fake = _pool_runner(monkeypatch)
    result = runner.invoke(cli, ["proxy", "pool", "add", "garbage"])
    assert result.exit_code != 0
    assert fake.members == set()


def test_proxy_pool_list_and_remove(monkeypatch):
    from scraper.cli import cli
    runner, fake = _pool_runner(monkeypatch)
    runner.invoke(cli, ["proxy", "pool", "add", "1.2.3.4:9999:user:pass"])
    listed = runner.invoke(cli, ["proxy", "pool", "list"])
    assert "1.2.3.4:9999" in listed.output
    removed = runner.invoke(cli, ["proxy", "pool", "remove", "1.2.3.4:9999:user:pass"])
    assert removed.exit_code == 0
    assert fake.members == set()


def test_run_discovery_us_only_pins_com_and_skips_detection(monkeypatch):
    import scraper.cli as c
    monkeypatch.setattr(c, "get_redis", lambda url: object())
    monkeypatch.setattr(c, "get_queue", lambda conn: object())
    monkeypatch.setattr(c, "load_unblocker_config", lambda conn: None)
    monkeypatch.setattr(c, "_load_cached_marketplaces", lambda urls: __import__("asyncio").sleep(0, result={}))
    monkeypatch.setattr(c, "detect_marketplace",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("detection must be skipped in us_only")))
    seen = {}

    def fake_get_urls(url, proxy_url=None, requests_per_second=0.5, max_pages=9999, max_challenge_retries=4):
        seen["url"] = url
        return ["https://www.ebay.com/itm/111111111111"]

    monkeypatch.setattr(c, "get_item_urls_from_store", fake_get_urls)
    monkeypatch.setattr(c, "enqueue_items", lambda *a, **k: 1)

    class S:
        redis_url = "redis://x"
        requests_per_second = 0.5

    ok, empty, blocked, unresolved, total = c._run_discovery(
        [("https://www.ebay.com/str/someseller", "auto")], "http://res", S(), us_only=True
    )
    assert ok == ["https://www.ebay.com/str/someseller"]
    assert seen["url"] == "https://www.ebay.com/str/someseller"

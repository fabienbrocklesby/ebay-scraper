from scraper.fetch import apply_proxy_country, expected_currency, is_challenge_page

_IPROYAL = "http://user:pass@geo.iproyal.com:12321"


def test_detects_security_measure_page():
    html = "<html><head><title>Security Measure | eBay</title></head><body></body></html>"
    assert is_challenge_page(html) is True


def test_detects_verify_yourself_body():
    html = "<html><body>Please verify yourself to continue</body></html>"
    assert is_challenge_page(html) is True


def test_detects_pardon_our_interruption_page():
    html = "<html><head><title>Pardon Our Interruption...</title></head></html>"
    assert is_challenge_page(html) is True


def test_detects_checking_your_browser_page():
    html = "<html><body>Checking your browser before you access eBay.</body></html>"
    assert is_challenge_page(html) is True


def test_real_product_page_is_not_a_challenge():
    html = '<html><head><title>Genuine OEM Toyota Brake Pads | eBay</title></head><body></body></html>'
    assert is_challenge_page(html) is False


def test_apply_proxy_country_us_for_dotcom():
    out = apply_proxy_country(_IPROYAL, "https://www.ebay.com/itm/123")
    assert out == "http://user:pass_country-us@geo.iproyal.com:12321"


def test_apply_proxy_country_au_for_com_au():
    out = apply_proxy_country(_IPROYAL, "https://www.ebay.com.au/str/seller")
    assert out == "http://user:pass_country-au@geo.iproyal.com:12321"


def test_apply_proxy_country_gb_for_co_uk():
    out = apply_proxy_country(_IPROYAL, "https://www.ebay.co.uk/itm/123")
    assert out == "http://user:pass_country-gb@geo.iproyal.com:12321"


def test_apply_proxy_country_respects_explicit_country():
    pinned = "http://user:pass_country-de@geo.iproyal.com:12321"
    assert apply_proxy_country(pinned, "https://www.ebay.com/itm/1") == pinned


def test_apply_proxy_country_ignores_non_iproyal_proxy():
    other = "http://user:pass@someproxy.example.com:8080"
    assert apply_proxy_country(other, "https://www.ebay.com.au/itm/1") == other


def test_apply_proxy_country_passthrough_when_no_proxy():
    assert apply_proxy_country(None, "https://www.ebay.com/itm/1") is None


def test_apply_proxy_country_unknown_domain_unchanged():
    assert apply_proxy_country(_IPROYAL, "https://example.com/x") == _IPROYAL


def test_expected_currency_known_sites():
    assert expected_currency("https://www.ebay.com/itm/1") == "USD"
    assert expected_currency("https://www.ebay.com.au/itm/1") == "AUD"
    assert expected_currency("https://www.ebay.co.uk/itm/1") == "GBP"
    assert expected_currency("https://www.ebay.de/itm/1") == "EUR"


def test_expected_currency_unknown_site():
    assert expected_currency("https://example.com/x") is None

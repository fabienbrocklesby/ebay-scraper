import respx
import httpx

from scraper.unblocker import UnblockerConfig, fetch_via_unblocker


OXY = "https://realtime.oxylabs.io/v1/queries"


def test_disabled_config_returns_none():
    cfg = UnblockerConfig(provider="none", username=None, password=None)
    assert cfg.enabled is False
    assert fetch_via_unblocker("https://www.ebay.com.au/str/x", cfg) is None


@respx.mock
def test_oxylabs_returns_html():
    respx.post(OXY).mock(
        return_value=httpx.Response(200, json={"results": [{"content": "<html>grid</html>"}]})
    )
    cfg = UnblockerConfig(provider="oxylabs", username="u", password="p")
    assert cfg.enabled is True
    html = fetch_via_unblocker("https://www.ebay.com.au/str/x", cfg)
    assert html == "<html>grid</html>"


@respx.mock
def test_oxylabs_non_200_returns_none():
    respx.post(OXY).mock(return_value=httpx.Response(500, json={}))
    cfg = UnblockerConfig(provider="oxylabs", username="u", password="p")
    assert fetch_via_unblocker("https://www.ebay.com.au/str/x", cfg) is None

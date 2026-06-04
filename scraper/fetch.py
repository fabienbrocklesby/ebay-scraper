from typing import Any, Optional
from urllib.parse import urlparse


class ChallengeError(Exception):
    """eBay served a bot-verification page instead of the requested content.

    eBay returns these interstitials with HTTP 200, so they cannot be detected
    by status code. They must never be treated as 'no more data': doing so
    silently truncates a store crawl or drops an item. Callers treat this as a
    transient failure and retry on a fresh IP (rotating proxy) or after a cooldown.
    """


# eBay serves at least two distinct bot interstitials, both with HTTP 200:
#   1. "Security Measure | eBay" - explicit "verify yourself" page.
#   2. "Pardon Our Interruption..." - a "checking your browser" redirect page.
# Each marker is specific enough never to match a real product or store page.
# Missing any one of these means that interstitial is read as 'no data' and the
# item or store page is silently dropped, so this list must stay exhaustive.
_CHALLENGE_MARKERS = (
    "Security Measure | eBay",
    "Please verify yourself to continue",
    "Pardon Our Interruption",
    "Checking your browser before you access",
)


def is_challenge_page(html: str) -> bool:
    return any(marker in html for marker in _CHALLENGE_MARKERS)


def build_session(proxy_url: Optional[str] = None) -> Any:
    """Create a curl_cffi session that impersonates Chrome's TLS fingerprint.

    eBay fingerprints the TLS handshake (JA3/JA4) to detect non-browser clients
    regardless of User-Agent or IP. curl_cffi reproduces Chrome's exact handshake,
    which a plain Python HTTP client cannot.
    """
    from curl_cffi.requests import Session

    kwargs: dict[str, Any] = {"impersonate": "chrome131"}
    if proxy_url:
        kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}
    return Session(**kwargs)


# Maps each eBay site's domain suffix (the part after "ebay.") to the ISO country
# code IPRoyal expects. eBay serves country-specific listings and pricing per
# domain, and an in-country exit IP is less likely to be redirected or challenged,
# so the proxy is pinned to the site's country automatically.
_EBAY_TLD_TO_COUNTRY = {
    "com": "us",
    "com.au": "au",
    "co.uk": "gb",
    "de": "de",
    "ca": "ca",
    "fr": "fr",
    "it": "it",
    "es": "es",
    "nl": "nl",
    "be": "be",
    "at": "at",
    "ch": "ch",
    "ie": "ie",
    "pl": "pl",
    "ph": "ph",
    "com.sg": "sg",
    "com.my": "my",
    "com.hk": "hk",
}


def _ebay_country_code(target_url: str) -> Optional[str]:
    host = urlparse(target_url).netloc.lower()
    marker = "ebay."
    idx = host.find(marker)
    if idx == -1:
        return None
    suffix = host[idx + len(marker):]
    return _EBAY_TLD_TO_COUNTRY.get(suffix)


def apply_proxy_country(proxy_url: Optional[str], target_url: str) -> Optional[str]:
    """Pin an IPRoyal proxy to the country of the target eBay site.

    For an IPRoyal endpoint, inject the matching `_country-XX` token into the
    password so the exit IP is in the same country as the eBay domain being
    scraped. A country the user pinned explicitly is left as-is, and non-IPRoyal
    proxies or unrecognised domains are returned unchanged. This is what lets one
    proxy credential scrape any country's eBay without per-site configuration.
    """
    if not proxy_url or "iproyal.com" not in proxy_url:
        return proxy_url
    if "_country-" in proxy_url:
        return proxy_url
    country = _ebay_country_code(target_url)
    if not country:
        return proxy_url
    parsed = urlparse(proxy_url)
    if not (parsed.username and parsed.password and parsed.hostname and parsed.port):
        return proxy_url
    userinfo = f"{parsed.username}:{parsed.password}_country-{country}"
    return f"{parsed.scheme}://{userinfo}@{parsed.hostname}:{parsed.port}"

import re
import time
from typing import Any
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs


def _sch_to_str_url(url: str) -> str | None:
    """Convert an eBay /sch/ seller-filtered URL to the equivalent /str/ store URL.

    eBay's /sch/ endpoint returns 403 to non-browser clients. The /str/ store
    pages are accessible and use a.str-item-card__link for item links.
    """
    parsed = urlparse(url)
    if "/sch/" not in parsed.path:
        return None
    ssn = parse_qs(parsed.query).get("_ssn", [None])[0]
    if not ssn:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/str/{ssn}"


def extract_seller_id(store_url: str) -> str:
    path = urlparse(store_url).path.strip("/")
    parts = path.split("/")
    if "str" in parts:
        return parts[parts.index("str") + 1]
    if "sch" in parts:
        return parts[parts.index("sch") + 1]
    raise ValueError(f"Cannot extract seller ID from: {store_url}")


def _normalize_store_url(store_url: str) -> str:
    """Return canonical store URL: scheme+host+path, no trailing slash, no query params."""
    parsed = urlparse(store_url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


def _homepage_url(store_url: str) -> str:
    parsed = urlparse(store_url)
    return f"{parsed.scheme}://{parsed.netloc}/"


def _extract_item_urls(html: str) -> list[str]:
    """Extract clean item URLs from a store listing page.

    eBay store pages use a.str-item-card__link for item links.
    Returns URLs with query params stripped, deduped by URL.
    """
    soup = BeautifulSoup(html, "html.parser")
    seen: dict[str, None] = {}
    for a in soup.select("a.str-item-card__link"):
        href = a.get("href", "")
        m = re.search(r"(https?://[^/?#]+/itm/\d+)", href)
        if m:
            seen[m.group(1)] = None
    return list(seen.keys())


def _has_next_page(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    return bool(soup.select_one("a.pagination__next"))


def get_item_urls_from_store(
    store_url: str,
    proxy_url: str | None = None,
    requests_per_second: float = 0.5,
    max_pages: int = 9999,
    _session: Any | None = None,
) -> list[str]:
    """Paginate through a seller store and return a deduplicated list of item URLs.

    eBay's search API (/sch/i.html) blocks programmatic access. This function
    paginates the store's own /str/ page (?_pgn=N), which is accessible.

    Uses curl_cffi to impersonate Chrome's TLS fingerprint (JA3/JA4), bypassing
    eBay's bot detection layer. A homepage warmup establishes session cookies.

    _session: inject an httpx.Client in tests so respx can mock network calls.
    Production always uses curl_cffi (pass nothing).

    Returns full item URLs (e.g. https://www.ebay.com.au/itm/123) so the
    correct domain is preserved for non-US eBay sites.
    """
    converted = _sch_to_str_url(store_url)
    if converted:
        store_url = converted
    base_store_url = _normalize_store_url(store_url)
    homepage = _homepage_url(store_url)
    delay = 1.0 / requests_per_second if requests_per_second > 0 else 2.0

    own_client = _session is None
    if own_client:
        from curl_cffi.requests import Session
        session_kwargs: dict[str, Any] = {"impersonate": "chrome131"}
        if proxy_url:
            session_kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}
        client = Session(**session_kwargs)
    else:
        client = _session

    seen: dict[str, None] = {}

    try:
        try:
            client.get(homepage)
        except Exception:
            pass
        time.sleep(max(0.5, delay))

        for page in range(1, max_pages + 1):
            page_url = (
                base_store_url if page == 1
                else f"{base_store_url}?_pgn={page}"
            )
            try:
                response = client.get(page_url)
                if response.status_code != 200:
                    break
            except Exception:
                break

            for url in _extract_item_urls(response.text):
                seen[url] = None

            if not _extract_item_urls(response.text) or not _has_next_page(response.text):
                break
            if page < max_pages:
                time.sleep(delay)
    finally:
        if own_client:
            client.close()

    return list(seen.keys())

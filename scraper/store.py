"""
eBay store crawler: paginates through a seller store and extracts item IDs.

Supports /str/ and /sch/ store URL formats. Uses httpx sync client with
optional proxy support (httpx >= 0.27 single `proxy=` parameter).
"""

import re
import time
import httpx
from bs4 import BeautifulSoup
from urllib.parse import urlparse, urlencode

EBAY_SEARCH_BASE = "https://www.ebay.com/sch/i.html"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def extract_seller_id(store_url: str) -> str:
    """Extract the seller ID from an eBay store or search URL.

    Handles:
      - https://www.ebay.com/str/<seller_id>
      - https://www.ebay.com/sch/<seller_id>/m.html
    """
    path = urlparse(store_url).path.strip("/")
    parts = path.split("/")
    if "str" in parts:
        return parts[parts.index("str") + 1]
    if "sch" in parts:
        return parts[parts.index("sch") + 1]
    raise ValueError(f"Cannot extract seller ID from URL: {store_url}")


def build_store_search_url(seller_id: str, page: int) -> str:
    """Build a paginated eBay search URL for a specific seller."""
    params = {
        "_ssn": seller_id,
        "_ipg": "240",
        "_pgn": str(page),
    }
    return f"{EBAY_SEARCH_BASE}?{urlencode(params)}"


def _extract_item_ids(html: str) -> list[str]:
    """Parse item IDs from eBay search results HTML."""
    soup = BeautifulSoup(html, "html.parser")
    ids = []
    for a in soup.select("a.s-item__link"):
        href = a.get("href", "")
        match = re.search(r"/itm/(\d+)", href)
        if match:
            ids.append(match.group(1))
    return ids


def _has_next_page(html: str) -> bool:
    """Return True if a pagination next-page link exists in the HTML."""
    soup = BeautifulSoup(html, "html.parser")
    return bool(soup.select_one("a.pagination__next"))


def get_item_ids_from_store(
    store_url: str,
    proxy_url: str | None = None,
    requests_per_second: float = 0.5,
    max_pages: int = 9999,
) -> list[str]:
    """Paginate through a seller store and return a deduplicated list of item IDs.

    Args:
        store_url: eBay store or search URL for the seller.
        proxy_url: Optional rotating proxy URL (e.g. http://user:pass@host:port).
                   Passed directly to httpx Client as proxy= (httpx >= 0.27 API).
        requests_per_second: Throttle rate. Defaults to 0.5 (one request per 2s).
        max_pages: Safety cap on pages fetched. Defaults to 9999 (effectively unlimited).

    Returns:
        Ordered list of unique item ID strings.
    """
    seller_id = extract_seller_id(store_url)
    delay = 1.0 / requests_per_second if requests_per_second > 0 else 2.0

    client_kwargs: dict = {"headers": HEADERS, "timeout": 30}
    if proxy_url:
        # httpx >= 0.27 uses proxy= as a single URL string, not proxies= dict
        client_kwargs["proxy"] = proxy_url

    seen: dict[str, None] = {}

    with httpx.Client(**client_kwargs) as client:
        for page in range(1, max_pages + 1):
            url = build_store_search_url(seller_id, page)
            response = client.get(url)
            response.raise_for_status()
            ids = _extract_item_ids(response.text)
            for item_id in ids:
                seen[item_id] = None
            if not ids or not _has_next_page(response.text):
                break
            if page < max_pages:
                time.sleep(delay)

    return list(seen.keys())

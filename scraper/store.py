import re
import time
import httpx
from bs4 import BeautifulSoup
from urllib.parse import urlparse

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
}


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
) -> list[str]:
    """Paginate through a seller store and return a deduplicated list of item URLs.

    eBay's search API (/sch/i.html) blocks programmatic access. This function
    paginates the store's own /str/ page (?_pgn=N), which is accessible.

    Warms the session by visiting the eBay homepage first so that cookies are
    established before hitting the store and item pages. This is required to
    avoid bot-detection blocks on item detail pages.

    Returns full item URLs (e.g. https://www.ebay.com.au/itm/123) so the
    correct domain is preserved for non-US eBay sites.
    """
    base_store_url = _normalize_store_url(store_url)
    homepage = _homepage_url(store_url)
    delay = 1.0 / requests_per_second if requests_per_second > 0 else 2.0

    client_kwargs: dict = {
        "headers": HEADERS,
        "timeout": 30,
        "follow_redirects": True,
    }
    if proxy_url:
        client_kwargs["proxy"] = proxy_url

    seen: dict[str, None] = {}

    with httpx.Client(**client_kwargs) as client:
        try:
            client.get(homepage)
        except httpx.HTTPError:
            pass
        time.sleep(max(0.5, delay))

        for page in range(1, max_pages + 1):
            page_url = (
                base_store_url if page == 1
                else f"{base_store_url}?_pgn={page}"
            )
            response = client.get(page_url)
            response.raise_for_status()

            for url in _extract_item_urls(response.text):
                seen[url] = None

            if not _extract_item_urls(response.text) or not _has_next_page(response.text):
                break
            if page < max_pages:
                time.sleep(delay)

    return list(seen.keys())

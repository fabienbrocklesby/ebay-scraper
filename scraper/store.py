import re
import time
from typing import Any
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs

from scraper.fetch import ChallengeError, apply_proxy_country, build_session, is_challenge_page

# eBay caps store pages at 240 items each. Requesting the maximum cuts the number
# of requests ~4x versus the default page size, which both speeds up a crawl and
# slows how quickly a single IP trips eBay's per-IP request throttle.
_ITEMS_PER_PAGE = 240

# eBay challenges a single IP after roughly 20-40 requests. With a rotating
# residential proxy a fresh session yields a fresh exit IP, so the crawl rebuilds
# its session every few pages to stay well under that threshold and keep going on
# large stores. Without a proxy (direct from the coordinator IP) rotating gains
# nothing, so it is skipped.
_PAGES_PER_SESSION = 10


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


def _page_url(base_store_url: str, page: int) -> str:
    return f"{base_store_url}?_pgn={page}&_ipg={_ITEMS_PER_PAGE}"


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


def _warmup(client: Any, homepage: str, delay: float) -> None:
    try:
        client.get(homepage)
    except Exception:
        pass
    time.sleep(max(0.5, delay))


def _fetch_listing_page(
    client: Any, page_url: str, homepage: str
) -> str | None:
    """Fetch one store listing page. Returns HTML, or None on transport error / non-200."""
    try:
        response = client.get(page_url, headers={"Referer": homepage})
    except Exception:
        return None
    if response.status_code != 200:
        return None
    return response.text


def _recover_from_challenge(
    old_client: Any,
    base_store_url: str,
    homepage: str,
    page: int,
    proxy_url: str | None,
    delay: float,
    max_retries: int,
    gathered: int,
) -> tuple[Any, str]:
    """Retry a challenged page on fresh sessions with escalating backoff.

    Each attempt builds a new session: with a rotating residential proxy this
    yields a fresh exit IP, which is what actually clears eBay's challenge.
    Returns (surviving_session, page_html) once clear so the caller can keep
    crawling on the working session.

    Raises:
        ChallengeError: still blocked after max_retries.
    """
    old_client.close()
    for attempt in range(1, max_retries + 1):
        time.sleep(min(30.0 * attempt, 180.0))
        client = build_session(proxy_url)
        _warmup(client, homepage, delay)
        html = _fetch_listing_page(client, _page_url(base_store_url, page), homepage)
        if html is not None and not is_challenge_page(html):
            return client, html
        client.close()
    raise ChallengeError(
        f"eBay blocked store pagination at page {page} after {max_retries} retries "
        f"({gathered} items gathered before the block). Configure a rotating "
        f"residential proxy (scraper proxy set <url>) or retry later."
    )


def get_item_urls_from_store(
    store_url: str,
    proxy_url: str | None = None,
    requests_per_second: float = 0.5,
    max_pages: int = 9999,
    max_challenge_retries: int = 4,
    _session: Any | None = None,
) -> list[str]:
    """Paginate a seller store and return a deduplicated list of item URLs.

    eBay's search API (/sch/i.html) blocks programmatic access. This function
    paginates the store's own /str/ page (?_pgn=N&_ipg=240), which is accessible.

    Uses curl_cffi to impersonate Chrome's TLS fingerprint (JA3/JA4), bypassing
    eBay's bot detection layer. A homepage warmup establishes session cookies.

    eBay serves a bot-challenge page (HTTP 200, title "Security Measure") once an
    IP exceeds its request threshold. That page is detected explicitly and is NOT
    treated as the end of the store. On a challenge the crawl backs off and retries
    on a fresh session (a new IP, if a rotating proxy is configured). If the
    challenge persists past max_challenge_retries, ChallengeError is raised with
    the count gathered so far, rather than silently returning a truncated list.

    _session: inject an httpx.Client in tests so respx can mock network calls.
    Production always uses curl_cffi (pass nothing).

    Returns full item URLs (e.g. https://www.ebay.com.au/itm/123) so the
    correct domain is preserved for non-US eBay sites.

    Raises:
        ChallengeError: eBay blocked the crawl and it could not be completed.
    """
    converted = _sch_to_str_url(store_url)
    if converted:
        store_url = converted
    base_store_url = _normalize_store_url(store_url)
    homepage = _homepage_url(store_url)
    delay = 1.0 / requests_per_second if requests_per_second > 0 else 2.0

    proxy_url = apply_proxy_country(proxy_url, store_url)
    own_client = _session is None
    rotating = own_client and bool(proxy_url)
    client = build_session(proxy_url) if own_client else _session

    seen: dict[str, None] = {}

    try:
        _warmup(client, homepage, delay)

        pages_on_session = 0
        page = 1
        while page <= max_pages:
            if rotating and pages_on_session >= _PAGES_PER_SESSION:
                client.close()
                client = build_session(proxy_url)
                _warmup(client, homepage, delay)
                pages_on_session = 0

            html = _fetch_listing_page(client, _page_url(base_store_url, page), homepage)
            pages_on_session += 1

            if html is None or is_challenge_page(html):
                if not own_client:
                    raise ChallengeError(
                        f"eBay challenge on page {page} ({len(seen)} items gathered)"
                    )
                client, html = _recover_from_challenge(
                    client, base_store_url, homepage, page, proxy_url, delay,
                    max_challenge_retries, len(seen),
                )
                pages_on_session = 1

            page_urls = _extract_item_urls(html)
            for url in page_urls:
                seen[url] = None

            if not page_urls or not _has_next_page(html):
                break
            time.sleep(delay)
            page += 1
    finally:
        if own_client:
            client.close()

    return list(seen.keys())

import re
import time
from typing import Any, Callable
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

# eBay intermittently serves a short/empty listing page mid-crawl (observed: a
# ~130KB page with 0 items wedged between two full pages). Treating that as the
# end of the store silently truncated a large catalogue, so an empty page is
# re-fetched (on a fresh IP when rotating) this many times before it is accepted
# as the genuine end of the store.
_EMPTY_PAGE_RETRIES = 2


def extract_seller_id(store_url: str) -> str:
    path = urlparse(store_url).path.strip("/")
    parts = path.split("/")
    if "str" in parts:
        return parts[parts.index("str") + 1]
    if "sch" in parts:
        return parts[parts.index("sch") + 1]
    raise ValueError(f"Cannot extract seller ID from: {store_url}")


def _normalize_store_url(store_url: str) -> str:
    """Return a canonical crawl URL for a store or seller-search input.

    A /str/ store page becomes scheme+host+path with no trailing slash or query.
    A /sch/ seller-search URL is kept as a seller search keyed on the seller name
    (_ssn), because a seller's store name can differ from their username, so
    converting to /str/{username} can land on a dead page. The seller search lists
    all of that seller's items regardless of how their store is named.
    """
    parsed = urlparse(store_url)
    if "/sch/" in parsed.path:
        ssn = parse_qs(parsed.query).get("_ssn", [None])[0]
        if ssn:
            return f"{parsed.scheme}://{parsed.netloc}/sch/i.html?_ssn={ssn}"
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


def _homepage_url(store_url: str) -> str:
    parsed = urlparse(store_url)
    return f"{parsed.scheme}://{parsed.netloc}/"


def _page_url(base_store_url: str, page: int) -> str:
    sep = "&" if "?" in base_store_url else "?"
    return f"{base_store_url}{sep}_pgn={page}&_ipg={_ITEMS_PER_PAGE}"


def _extract_item_urls(html: str) -> list[str]:
    """Extract clean item URLs from a listing page, deduped, query stripped.

    Store pages use a.str-item-card__link. Seller-search pages and storefront
    variants use different markup, so when the store-card selector finds nothing,
    fall back to every /itm/ link on the page. This keeps discovery working for
    any seller URL the user adds, not just standard /str/ store grids.
    """
    soup = BeautifulSoup(html, "html.parser")
    seen: dict[str, None] = {}
    for a in soup.select("a.str-item-card__link"):
        href = a.get("href", "")
        m = re.search(r"(https?://[^/?#]+/itm/\d+)", href)
        if m:
            seen[m.group(1)] = None
    if not seen:
        for m in re.finditer(r"https?://[^/?#\"']+/itm/(\d{11,13})", html):
            seen[f"https://{urlparse(m.group(0)).netloc}/itm/{m.group(1)}"] = None
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
    """Retry a challenged page on fresh sessions with a short escalating backoff.

    Each attempt builds a new session: with a rotating residential proxy this
    yields a fresh exit IP, which is what actually clears eBay's challenge. Because
    a fresh IP is available immediately, the wait is kept short (a long cooldown
    only helps a single fixed IP); a store that stays challenged across several
    fresh IPs is deferred quickly rather than wedging the crawl for minutes.
    Returns (surviving_session, page_html) once clear so the caller can keep
    crawling on the working session.

    Raises:
        ChallengeError: still blocked after max_retries.
    """
    old_client.close()
    for attempt in range(1, max_retries + 1):
        time.sleep(min(5.0 * attempt, 20.0))
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


def _retry_empty_page(
    client: Any,
    base_store_url: str,
    homepage: str,
    page: int,
    proxy_url: str | None,
    delay: float,
    rotating: bool,
    max_retries: int,
) -> tuple[Any, str | None, list[str]]:
    """Re-fetch a page that returned no items, to tell a transient empty page apart
    from the real end of the store.

    With a rotating proxy each retry rebuilds the session for a fresh exit IP (the
    usual cause of a degraded page); otherwise the same session is reused. Returns
    (client, html, item_urls): item_urls is non-empty when a retry recovers items
    and the crawl should continue, or empty when the store has genuinely ended.
    """
    for _ in range(max_retries):
        time.sleep(delay)
        if rotating:
            client.close()
            client = build_session(proxy_url)
            _warmup(client, homepage, delay)
        html = _fetch_listing_page(client, _page_url(base_store_url, page), homepage)
        if html is not None and not is_challenge_page(html):
            urls = _extract_item_urls(html)
            if urls:
                return client, html, urls
    return client, None, []


# eBay stops paginating store/search browse around 10 000 results. When a crawl
# hits this ceiling, re-crawling the same store filtered by non-overlapping price
# bands recovers items that browse pagination hides.
_BROWSE_CEILING = 10000


def _price_partitions(lo: float, hi: float, splits: int = 4) -> list[tuple[float, float]]:
    """Divide [lo, hi) into `splits` equal sub-ranges for price-filtered re-crawls."""
    step = (hi - lo) / splits
    return [(round(lo + step * i, 2), round(lo + step * (i + 1), 2)) for i in range(splits)]


def _partition_url(base_store_url: str, lo: float, hi: float) -> str:
    """Append eBay price-range filter params to a store URL."""
    sep = "&" if "?" in base_store_url else "?"
    return f"{base_store_url}{sep}_udlo={lo}&_udhi={hi}"


def _crawl_pages(
    client: Any,
    base_url: str,
    homepage: str,
    max_pages: int,
    delay: float,
) -> list[str]:
    """Fetch listing pages for `base_url` (already has any filter params) and return item URLs.

    Simplified inner loop used by the partitioned re-crawl path. It does not rotate
    sessions, but it must not swallow a challenge: reading a challenge page as
    "no more items" would silently truncate the partition and drop products. So a
    challenge is raised loudly, letting the caller report the store as blocked.
    """
    seen: dict[str, None] = {}
    page = 1
    while page <= max_pages:
        page_url = _page_url(base_url, page)
        html = _fetch_listing_page(client, page_url, homepage)
        if html is None:
            break
        if is_challenge_page(html):
            raise ChallengeError(
                f"eBay challenge during partitioned crawl of {base_url}, page {page}"
            )
        for url in _extract_item_urls(html):
            seen[url] = None
        if not _has_next_page(html):
            break
        time.sleep(delay)
        page += 1
    return list(seen.keys())


_ITM_RE = re.compile(r"/itm/(\d{11,13})")
_PRICE_RE = re.compile(r"[\d][\d,]*\.\d{2}")


def parse_listing_cards(html: str) -> list[tuple[str, float | None, str]]:
    """Extract (item_id, price, item_url) from each store listing card.

    eBay store cards use the container class `.str-item-card`. A few promo/header
    cards carry no item link and are skipped. Price is parsed from the card text
    (first currency-looking number); None if the card shows no price.
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[str, float | None, str]] = []
    seen: set[str] = set()
    for card in soup.select(".str-item-card"):
        link = card.find("a", href=_ITM_RE)
        if not link:
            continue
        m = _ITM_RE.search(link["href"])
        if not m:
            continue
        item_id = m.group(1)
        if item_id in seen:
            continue
        seen.add(item_id)
        pm = _PRICE_RE.search(card.get_text(" ", strip=True))
        price = float(pm.group(0).replace(",", "")) if pm else None
        url = f"https://www.ebay.com.au/itm/{item_id}"
        out.append((item_id, price, url))
    return out


def get_store_listings(
    store_url: str,
    proxy_url: str | None = None,
    max_pages: int = 9999,
    max_challenge_retries: int = 4,
    _session: Any | None = None,
) -> list[tuple[str, float | None, str]]:
    """Crawl store listing pages, returning (item_id, price, item_url) per card.

    Cheaper than get_item_urls_from_store for delta: one request yields ~200
    items with their current price, no detail-page hits.
    """
    base = _normalize_store_url(store_url)
    session = _session or build_session(apply_proxy_country(proxy_url, base))
    collected: dict[str, tuple[str, float | None, str]] = {}
    page = 1
    while page <= max_pages:
        html = session.get(_page_url(base, page), timeout=30).text
        if is_challenge_page(html):
            raise ChallengeError(
                f"challenge on listing page {page}, gathered {len(collected)}"
            )
        cards = parse_listing_cards(html)
        if not cards:
            break
        for item_id, price, url in cards:
            collected.setdefault(item_id, (item_id, price, url))
        if not _has_next_page(html):
            break
        page += 1
    return list(collected.values())


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

            if not page_urls and own_client:
                client, html, page_urls = _retry_empty_page(
                    client, base_store_url, homepage, page, proxy_url, delay,
                    rotating, _EMPTY_PAGE_RETRIES,
                )
                pages_on_session = 1

            for url in page_urls:
                seen[url] = None

            if not page_urls or not _has_next_page(html):
                break
            time.sleep(delay)
            page += 1
    finally:
        if own_client:
            client.close()

    # When the normal crawl hits eBay's browse ceiling (~10 000 results), the
    # result set is artificially truncated. Re-crawl with non-overlapping price
    # bands to recover hidden items. One level of partitioning is enough in
    # practice; recursion is not needed and is deliberately avoided.
    # Skip when _session is injected (unit tests) to keep tests simple.
    if own_client and len(seen) >= _BROWSE_CEILING:
        extra_client = build_session(proxy_url)
        try:
            for lo, hi in _price_partitions(0, _BROWSE_CEILING):
                part_base = _partition_url(base_store_url, lo, hi)
                for url in _crawl_pages(extra_client, part_base, homepage, max_pages, delay):
                    seen[url] = None
        finally:
            extra_client.close()

    return list(seen.keys())


def _paginate_via(
    fetch_fn: Callable[[str], str | None], base_url: str, max_pages: int
) -> dict[str, None]:
    """Paginate a listing URL using a caller-supplied page fetcher.

    fetch_fn(page_url) returns the page HTML, or None on failure. A None page, a
    challenge page, or a page with no items ends pagination, exactly as the proxy
    crawl treats those, so a degraded response never reads as a clean end-of-store.
    """
    seen: dict[str, None] = {}
    page = 1
    while page <= max_pages:
        html = fetch_fn(_page_url(base_url, page))
        if not html or is_challenge_page(html):
            break
        page_urls = _extract_item_urls(html)
        if not page_urls:
            break
        for url in page_urls:
            seen[url] = None
        if not _has_next_page(html):
            break
        page += 1
    return seen


def get_item_urls_via_unblocker(
    store_url: str,
    unblock_fetch: Callable[[str], str | None],
    max_pages: int = 9999,
) -> list[str]:
    """Discover a seller's item URLs through a paid-unblocker fetch callable.

    eBay challenges the residential proxy on its seller-search surface for some
    countries (notably US/UK on a flagged pool) while still serving item-detail
    pages to that same proxy. When the normal crawl is blocked, discovery escalates
    here: only the seller-search pagination (a small fraction of total requests)
    runs through the unblocker; item-detail fetches stay on the cheap proxy. Mirrors
    get_item_urls_from_store's price-partitioning past eBay's ~10k browse ceiling so
    large stores are not truncated.
    """
    base_store_url = _normalize_store_url(store_url)
    seen = _paginate_via(unblock_fetch, base_store_url, max_pages)
    if len(seen) >= _BROWSE_CEILING:
        for lo, hi in _price_partitions(0, _BROWSE_CEILING):
            seen.update(
                _paginate_via(unblock_fetch, _partition_url(base_store_url, lo, hi), max_pages)
            )
    return list(seen.keys())

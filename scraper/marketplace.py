"""Detect which eBay marketplace (domain + country) a seller's items live on.

eBay serves a seller's full item grid only on their home marketplace domain, to an
in-country viewer. A non-US seller crawled on ebay.com returns a genuine "0 results".
We therefore probe the candidate marketplaces for each seller and adopt the domain that
returns the most items. A bot-challenge during a probe is retried, never read as zero,
because conflating the two would mis-detect a seller's home as empty.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlparse

from scraper.fetch import ChallengeError, apply_proxy_country, build_session, is_challenge_page
from scraper.store import _extract_item_urls

# Ordered list of the marketplaces Kieran's stores live on. Extend as new ones appear.
CANDIDATE_MARKETPLACES: list[tuple[str, str]] = [
    ("www.ebay.com", "us"),
    ("www.ebay.com.au", "au"),
    ("www.ebay.co.uk", "gb"),
]

# Detection only needs an item-count signal, so request a small page to limit bandwidth.
_DETECT_ITEMS_PER_PAGE = 60
_MAX_PROBE_RETRIES = 3

FetchFn = Callable[[str, Optional[str]], str]


@dataclass(frozen=True)
class MarketplaceResult:
    domain: str
    country: str
    item_count: int
    seller_search_url: str


@dataclass(frozen=True)
class DetectionOutcome:
    """result is the best domain with items, or None. undetermined_domains are domains
    still challenged after retries, so the caller can escalate rather than treat as empty.
    """

    result: Optional[MarketplaceResult]
    undetermined_domains: list[str]


def _seller_search_url(domain: str, seller_id: str) -> str:
    return (
        f"https://{domain}/sch/i.html?_ssn={seller_id}"
        f"&_pgn=1&_ipg={_DETECT_ITEMS_PER_PAGE}"
    )


def _default_fetch(url: str, proxy_url: Optional[str]) -> str:
    pinned = apply_proxy_country(proxy_url, url)
    session = build_session(pinned)
    host = urlparse(url).netloc
    try:
        session.get(f"https://{host}/", timeout=30)  # warm the session (cold = 403)
        resp = session.get(url, timeout=40)
    except Exception as exc:  # network/proxy failure: treat as a soft block, let caller retry
        raise ChallengeError(f"probe network error for {url}: {exc}") from exc
    return resp.text


@dataclass(frozen=True)
class _Probe:
    domain: str
    country: str
    item_count: int
    url: str
    challenged: bool


def _probe_domain(
    seller_id: str, domain: str, country: str, proxy_url: Optional[str], fetch_fn: FetchFn
) -> _Probe:
    url = _seller_search_url(domain, seller_id)
    for _ in range(_MAX_PROBE_RETRIES):
        try:
            html = fetch_fn(url, proxy_url)
        except ChallengeError:
            continue
        if is_challenge_page(html):
            continue
        return _Probe(domain, country, len(_extract_item_urls(html)), url, challenged=False)
    return _Probe(domain, country, 0, url, challenged=True)


def detect_marketplace(
    seller_id: str,
    proxy_url: Optional[str],
    fetch_fn: FetchFn = _default_fetch,
    candidates: list[tuple[str, str]] = CANDIDATE_MARKETPLACES,
) -> DetectionOutcome:
    with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
        probes = list(
            pool.map(
                lambda c: _probe_domain(seller_id, c[0], c[1], proxy_url, fetch_fn),
                candidates,
            )
        )

    with_items = [p for p in probes if not p.challenged and p.item_count > 0]
    undetermined = [p.domain for p in probes if p.challenged]

    if with_items:
        best = max(with_items, key=lambda p: p.item_count)
        return DetectionOutcome(
            result=MarketplaceResult(best.domain, best.country, best.item_count, best.url),
            undetermined_domains=undetermined,
        )
    return DetectionOutcome(result=None, undetermined_domains=undetermined)

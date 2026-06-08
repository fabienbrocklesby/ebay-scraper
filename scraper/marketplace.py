"""Detect which eBay marketplace (domain + country) a seller's items live on.

eBay serves a seller's full item grid only on their home marketplace domain, to an
in-country viewer. A non-US seller crawled on ebay.com returns a genuine "0 results".
We therefore probe the candidate marketplaces for each seller and adopt the domain that
returns the most items. A bot-challenge during a probe is retried, never read as zero,
because conflating the two would mis-detect a seller's home as empty.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

from scraper.fetch import ChallengeError, apply_proxy_country, build_session, is_challenge_page
from scraper.store import _extract_item_urls

# Ordered in home-marketplace priority. eBay surfaces a seller's items on several
# marketplaces (cross-border listings), each capped at one ~200-item page, so a tie at
# the cap is normal, not ambiguous. Probes run concurrently; extend as new seller
# countries appear. Order matters: ties break toward the earlier (more likely home)
# domain, since `detect_marketplace` keeps the first domain that reaches the top count.
CANDIDATE_MARKETPLACES: list[tuple[str, str]] = [
    ("www.ebay.com", "us"),
    ("www.ebay.com.au", "au"),
    ("www.ebay.co.uk", "gb"),
    ("www.ebay.de", "de"),
    ("www.ebay.ca", "ca"),
]

# Detection picks the domain with the most items, so the page size must be large enough
# that a true home (hundreds of items) outscores a thin cross-listing; 60 caps every
# successful probe at 60 and destroys that signal.
_DETECT_ITEMS_PER_PAGE = 240
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


def _seller_search_url(domain: str, store_slug: str) -> str:
    # eBay challenges /sch seller-search from proxies but serves /str storefront
    # pages freely, so detection probes the storefront (and the crawl uses it too).
    # The id threaded through is the store slug: extract_seller_id returns the
    # /str/<slug>, so feeding storefront URLs keeps this consistent end to end.
    return f"https://{domain}/str/{store_slug}?_pgn=1&_ipg={_DETECT_ITEMS_PER_PAGE}"


def _default_fetch(url: str, proxy_url: Optional[str]) -> str:
    pinned = apply_proxy_country(proxy_url, url)
    session = build_session(pinned)
    host = urlparse(url).netloc
    try:
        session.get(f"https://{host}/", timeout=30)  # warm the session (cold = 403)
        resp = session.get(url, timeout=40)
    except Exception as exc:  # network/proxy errors become the unified ChallengeError retry signal so _probe_domain stays agnostic to failure type
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
        except ChallengeError as exc:
            logger.debug("probe challenged, retrying: %s", exc)
            continue
        if is_challenge_page(html):
            continue
        return _Probe(domain, country, len(_extract_item_urls(html)), url, challenged=False)
    logger.debug("domain undetermined after retries: %s", domain)
    return _Probe(domain, country, 0, url, challenged=True)


def detect_marketplace(
    seller_id: str,
    proxy_url: Optional[str],
    fetch_fn: FetchFn = _default_fetch,
    candidates: Optional[list[tuple[str, str]]] = None,
) -> DetectionOutcome:
    if candidates is None:
        candidates = CANDIDATE_MARKETPLACES
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
        # max keeps the first domain reaching the top count, and candidates are in
        # home priority order, so a cross-listing tie at the page cap breaks toward the
        # most likely home (.com for US sellers, else .com.au, ...). Currency is recorded
        # per item downstream, so this picks for catalogue completeness, not currency.
        best = max(with_items, key=lambda p: p.item_count)
        return DetectionOutcome(
            result=MarketplaceResult(best.domain, best.country, best.item_count, best.url),
            undetermined_domains=undetermined,
        )
    return DetectionOutcome(result=None, undetermined_domains=undetermined)

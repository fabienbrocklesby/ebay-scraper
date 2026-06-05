"""Optional paid unblocker fallback (Oxylabs eBay source).

OFF by default. Active only when Oxylabs credentials are configured on the coordinator.
Used as a last resort for a store that still fails through the residential proxy after
marketplace routing. There is no hard budget cap (the operator owns the cost), but every
call increments a Redis counter so spend is visible in run summaries.
"""

from dataclasses import dataclass
from typing import Any, Optional

import httpx

from scraper.fetch import _ebay_country_code

OXYLABS_ENDPOINT = "https://realtime.oxylabs.io/v1/queries"

UNBLOCKER_PROVIDER_KEY = "ebay-scraper:unblocker_provider"
OXYLABS_USER_KEY = "ebay-scraper:oxylabs_username"
OXYLABS_PASS_KEY = "ebay-scraper:oxylabs_password"
UNBLOCKER_COUNT_KEY = "ebay-scraper:unblocker_request_count"

_COUNTRY_NAME = {"us": "United States", "au": "Australia", "gb": "United Kingdom"}


@dataclass(frozen=True)
class UnblockerConfig:
    provider: str
    username: Optional[str]
    password: Optional[str]

    @property
    def enabled(self) -> bool:
        return self.provider == "oxylabs" and bool(self.username) and bool(self.password)


def load_unblocker_config(redis_conn: Any) -> UnblockerConfig:
    def _get(key: str) -> Optional[str]:
        raw = redis_conn.get(key)
        return raw.decode().strip() if raw else None

    return UnblockerConfig(
        provider=_get(UNBLOCKER_PROVIDER_KEY) or "none",
        username=_get(OXYLABS_USER_KEY),
        password=_get(OXYLABS_PASS_KEY),
    )


def fetch_via_unblocker(url: str, config: UnblockerConfig, redis_conn: Any = None) -> Optional[str]:
    if not config.enabled:
        return None
    country = _ebay_country_code(url)
    payload = {
        "source": "ebay",
        "url": url,
        "render": "html",
        "geo_location": _COUNTRY_NAME.get(country, "United States"),
    }
    try:
        resp = httpx.post(
            OXYLABS_ENDPOINT,
            auth=(config.username, config.password),
            json=payload,
            timeout=120,
        )
    except httpx.HTTPError:
        return None
    if redis_conn is not None:
        redis_conn.incr(UNBLOCKER_COUNT_KEY)
    if resp.status_code != 200:
        return None
    try:
        results = resp.json().get("results") or []
    except ValueError:  # malformed / non-JSON 200 body from the unblocker
        return None
    return results[0].get("content") if results else None

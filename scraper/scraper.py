import json
import re
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from scraper.fetch import (
    ChallengeError,
    WrongCountryError,
    apply_proxy_country,
    build_session,
    expected_currency,
    is_challenge_page,
)


def _clean_text(raw: str) -> str:
    """Normalise an eBay JSON-LD text value.

    eBay's JSON-LD `name` arrives with HTML entities (e.g. &#034;) and stray
    inline tags (e.g. <wbr> word-break hints). Parsing it as HTML strips the
    tags and decodes the entities in one pass, yielding clean CSV text.
    """
    if not raw:
        return ""
    return BeautifulSoup(raw, "html.parser").get_text()


@dataclass
class ProductData:
    item_id: str
    title: str
    price: float
    currency: str
    condition: str
    description: str
    image_urls: str
    item_url: str
    seller_id: str
    category: str
    item_specifics: str
    mpn: str
    upc: str
    shipping: str
    listing_type: str


def _item_id_from_url(item_url: str) -> str:
    m = re.search(r"/itm/(\d+)", item_url)
    if not m:
        raise ValueError(f"Cannot extract item ID from URL: {item_url}")
    return m.group(1)


def _condition_from_schema(schema_url: str) -> str:
    mapping = {
        "NewCondition": "New",
        "UsedCondition": "Used",
        "RefurbishedCondition": "Refurbished",
        "ForParts": "For Parts",
    }
    for key, val in mapping.items():
        if key in schema_url:
            return val
    return schema_url.split("/")[-1] if schema_url else ""


def _extract_item_specifics(soup: BeautifulSoup) -> str:
    specifics: dict[str, str] = {}
    section = soup.select_one("div.ux-layout-section--features")
    if section:
        for dt, dd in zip(section.select("dt"), section.select("dd")):
            key = dt.get_text(strip=True)
            val = dd.get_text(strip=True)
            if key and val:
                specifics[key] = val
    return json.dumps(specifics)


def _extract_identifier(specifics_json: str, *keys: str) -> str:
    try:
        specifics = json.loads(specifics_json)
    except (json.JSONDecodeError, TypeError):
        return ""
    for key in keys:
        val = specifics.get(key, "")
        if val:
            return val
    return ""


def _extract_shipping(soup: BeautifulSoup) -> str:
    for span in soup.select("span.ux-textspans--BOLD"):
        text = span.get_text(strip=True)
        if "ship" in text.lower() or "free" in text.lower():
            return text
    return ""


def _extract_listing_type(soup: BeautifulSoup) -> str:
    for span in soup.select("span.ux-textspans"):
        text = span.get_text(strip=True)
        if "Buy It Now" in text:
            return "Buy It Now"
        if "Auction" in text:
            return "Auction"
    return ""


def _fetch_description(soup: BeautifulSoup, client: Any, item_url: str) -> str:
    """Fetch the item description from eBay's separate description iframe.

    eBay serves item descriptions from itm.ebaydesc.com in an iframe, not in
    the main page HTML or JSON-LD. The iframe src is in the page and includes
    session parameters, so we reuse the existing client (with cookies).
    """
    iframe = soup.select_one("iframe[src*='ebaydesc.com']")
    if not iframe:
        return ""
    src = iframe.get("src", "")
    if not src:
        return ""
    try:
        resp = client.get(src, headers={"Referer": item_url})
        if resp.status_code != 200:
            return ""
        desc_soup = BeautifulSoup(resp.text, "html.parser")
        body = desc_soup.find("body")
        return body.get_text(separator=" ", strip=True) if body else ""
    except Exception:
        return ""


def _find_product_ld(soup: BeautifulSoup) -> Optional[dict]:
    """Find the JSON-LD Product block in page HTML.

    eBay embeds product data as JSON-LD. The block may be a top-level dict
    or inside an array (observed on ebay.com.au). Both forms are handled here.
    """
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, AttributeError):
            continue

        if isinstance(data, dict) and data.get("@type") == "Product":
            return data
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("@type") == "Product":
                    return item
    return None


def _extract_gallery_images(soup: BeautifulSoup, ld_json: dict) -> list[str]:
    """Return the full product image gallery as full-resolution URLs.

    eBay's JSON-LD `image` field is only a partial set (often about five). The
    complete gallery lives in the image carousel, where each photo appears at
    several sizes under one hash. We take the distinct hashes in carousel order
    and rewrite each to the largest size eBay serves (s-l1600.jpg). If the
    carousel is absent (page structure changed), fall back to the JSON-LD images.
    """
    hashes: list[str] = []
    seen: set[str] = set()
    for img in soup.select(".ux-image-carousel-item img, .ux-image-grid-item img"):
        src = img.get("src") or img.get("data-src") or ""
        match = re.search(r"/images/g/([^/]+)/", src)
        if match and match.group(1) not in seen:
            seen.add(match.group(1))
            hashes.append(match.group(1))
    if hashes:
        return [f"https://i.ebayimg.com/images/g/{h}/s-l1600.jpg" for h in hashes]
    images = ld_json.get("image", [])
    if isinstance(images, str):
        images = [images]
    return list(images)


def scrape_item(
    item_url: str,
    proxy_url: Optional[str] = None,
    client: Optional[Any] = None,
) -> Optional[ProductData]:
    """Fetch an eBay item page and return structured product data.

    Accepts a full item URL (e.g. https://www.ebay.com.au/itm/123456789) so that
    non-US eBay domains are handled correctly.

    When no client is provided, creates a curl_cffi session that impersonates
    Chrome's TLS fingerprint. eBay uses JA3/JA4 TLS fingerprinting to detect
    Python scrapers regardless of User-Agent or IP. curl_cffi bypasses this by
    using the exact TLS handshake Chrome produces. A homepage warmup request
    establishes session cookies before the item page fetch.

    Pass client explicitly only in tests (inject an httpx.Client for respx mocking).

    Returns None only when the item genuinely yields no data (404, or a page with
    no parseable product block). A bot-challenge or transport/server error is
    raised, not swallowed: returning None would make rq record the job as a
    successful empty scrape and drop the item silently.

    Raises:
        ChallengeError: eBay served a bot-verification page (HTTP 200) instead of
            the item. The job should be retried later or on a fresh proxy IP.
        Exception: transport error or 5xx, so rq can retry the job.
    """
    parsed = urlparse(item_url)
    homepage = f"{parsed.scheme}://{parsed.netloc}/"
    own_client = client is None

    if own_client:
        client = build_session(apply_proxy_country(proxy_url, item_url))

    try:
        if own_client:
            try:
                client.get(homepage)
            except Exception:
                pass
            time.sleep(1.0)

        response = client.get(
            item_url,
            headers={
                "Referer": homepage,
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
            },
        )
        if response.status_code == 404:
            return None
        if response.status_code == 403:
            raise ChallengeError(f"eBay 403 block fetching item {item_url}")
        response.raise_for_status()

        if is_challenge_page(response.text):
            raise ChallengeError(f"eBay challenge fetching item {item_url}")

        soup = BeautifulSoup(response.text, "html.parser")
        ld_json = _find_product_ld(soup)
        if not ld_json:
            return None

        offers = ld_json.get("offers", {})
        if isinstance(offers, list):
            offers = offers[0] if offers else {}

        images = _extract_gallery_images(soup, ld_json)

        item_specifics = _extract_item_specifics(soup)
        item_id = _item_id_from_url(item_url)
        description = _fetch_description(soup, client, item_url)

        currency = offers.get("priceCurrency", "")
        expected = expected_currency(item_url)
        if proxy_url and expected and currency and currency != expected:
            raise WrongCountryError(
                f"{item_url} returned {currency}, expected {expected} - proxy IP "
                f"resolved to the wrong country; retrying on a fresh IP"
            )

        return ProductData(
            item_id=item_id,
            title=_clean_text(ld_json.get("name", "")),
            price=float(offers.get("price", 0) or 0),
            currency=currency,
            condition=_condition_from_schema(offers.get("itemCondition", "")),
            description=description,
            image_urls="|".join(images),
            item_url=item_url,
            seller_id=(offers.get("seller") or {}).get("name", ""),
            category=ld_json.get("category", ""),
            item_specifics=item_specifics,
            mpn=_extract_identifier(item_specifics, "MPN", "Manufacturer Part Number", "mpn"),
            upc=_extract_identifier(item_specifics, "UPC", "EAN", "ISBN", "upc"),
            shipping=_extract_shipping(soup),
            listing_type=_extract_listing_type(soup),
        )
    finally:
        if own_client and client:
            client.close()

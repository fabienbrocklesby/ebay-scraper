import json
import re
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

from bs4 import BeautifulSoup


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


def scrape_item(
    item_url: str,
    proxy_url: Optional[str] = None,
    client: Optional[Any] = None,
) -> Optional[ProductData]:
    """Fetch an eBay item page and return structured product data, or None on failure.

    Accepts a full item URL (e.g. https://www.ebay.com.au/itm/123456789) so that
    non-US eBay domains are handled correctly.

    When no client is provided, creates a curl_cffi session that impersonates
    Chrome's TLS fingerprint. eBay uses JA3/JA4 TLS fingerprinting to detect
    Python scrapers regardless of User-Agent or IP. curl_cffi bypasses this by
    using the exact TLS handshake Chrome produces. A homepage warmup request
    establishes session cookies before the item page fetch.

    Pass client explicitly only in tests (inject an httpx.Client for respx mocking).
    """
    parsed = urlparse(item_url)
    homepage = f"{parsed.scheme}://{parsed.netloc}/"
    own_client = client is None

    try:
        if own_client:
            from curl_cffi.requests import Session
            session_kwargs: dict[str, Any] = {"impersonate": "chrome131"}
            if proxy_url:
                session_kwargs["proxies"] = {"http": proxy_url, "https": proxy_url}
            client = Session(**session_kwargs)
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
        response.raise_for_status()
    except Exception:
        return None
    finally:
        if own_client and client:
            client.close()

    soup = BeautifulSoup(response.text, "html.parser")
    ld_json = _find_product_ld(soup)
    if not ld_json:
        return None

    offers = ld_json.get("offers", {})
    if isinstance(offers, list):
        offers = offers[0] if offers else {}

    images = ld_json.get("image", [])
    if isinstance(images, str):
        images = [images]

    item_specifics = _extract_item_specifics(soup)
    item_id = _item_id_from_url(item_url)

    return ProductData(
        item_id=item_id,
        title=ld_json.get("name", ""),
        price=float(offers.get("price", 0) or 0),
        currency=offers.get("priceCurrency", ""),
        condition=_condition_from_schema(offers.get("itemCondition", "")),
        description=str(ld_json.get("description", "")),
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

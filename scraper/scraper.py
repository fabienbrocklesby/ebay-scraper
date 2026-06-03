import json
from dataclasses import dataclass
from typing import Optional

import httpx
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


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


def scrape_item(
    item_id: str,
    proxy_url: Optional[str] = None,
    client: Optional[httpx.Client] = None,
) -> Optional[ProductData]:
    url = f"https://www.ebay.com/itm/{item_id}"
    own_client = client is None

    try:
        if own_client:
            client_kwargs: dict = {"headers": HEADERS, "timeout": 30}
            if proxy_url:
                client_kwargs["proxy"] = proxy_url
            client = httpx.Client(**client_kwargs)
        response = client.get(url)
        if response.status_code == 404:
            return None
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    finally:
        if own_client and client:
            client.close()

    soup = BeautifulSoup(response.text, "html.parser")
    ld_json = None
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
            if isinstance(data, dict) and data.get("@type") == "Product":
                ld_json = data
                break
        except (json.JSONDecodeError, AttributeError):
            continue

    if not ld_json:
        return None

    offers = ld_json.get("offers", {})
    images = ld_json.get("image", [])
    if isinstance(images, str):
        images = [images]

    item_specifics = _extract_item_specifics(soup)

    return ProductData(
        item_id=item_id,
        title=ld_json.get("name", ""),
        price=float(offers.get("price", 0)),
        currency=offers.get("priceCurrency", ""),
        condition=_condition_from_schema(offers.get("itemCondition", "")),
        description=str(ld_json.get("description", "")),
        image_urls="|".join(images),
        item_url=url,
        seller_id=(offers.get("seller") or {}).get("name", ""),
        category=ld_json.get("category", ""),
        item_specifics=item_specifics,
        mpn=_extract_identifier(item_specifics, "MPN", "Manufacturer Part Number", "mpn"),
        upc=_extract_identifier(item_specifics, "UPC", "EAN", "ISBN", "upc"),
        shipping=_extract_shipping(soup),
        listing_type=_extract_listing_type(soup),
    )

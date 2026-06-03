import json
import pytest
import respx
import httpx
from scraper.scraper import scrape_item, ProductData

ITEM_URL = "https://www.ebay.com/itm/123456789"
HOMEPAGE_URL = "https://www.ebay.com/"

SAMPLE_HTML = """
<html>
<head>
<script type="application/ld+json">
{
  "@type": "Product",
  "name": "Genuine OEM Toyota Brake Pads",
  "description": "<p>Brand new genuine brake pads</p>",
  "image": [
    "https://i.ebayimg.com/images/g/abc/s-l1600.jpg",
    "https://i.ebayimg.com/images/g/def/s-l1600.jpg"
  ],
  "offers": {
    "@type": "Offer",
    "price": "45.99",
    "priceCurrency": "USD",
    "itemCondition": "https://schema.org/NewCondition",
    "seller": {"@type": "Person", "name": "autopartsking"}
  },
  "category": "Auto Parts"
}
</script>
</head>
<body>
<div class="ux-layout-section--features">
  <dl>
    <dt>Brand</dt><dd>Toyota</dd>
    <dt>Part Number</dt><dd>04465-33130</dd>
  </dl>
</div>
<span class="ux-textspans--BOLD">Free shipping</span>
<span class="ux-textspans">Buy It Now</span>
</body>
</html>
"""

AU_ARRAY_HTML = """
<html>
<head>
<script type="application/ld+json">
[
  {"@type": "Product",
   "name": "AU Product",
   "description": "A product",
   "image": ["https://i.ebayimg.com/au.jpg"],
   "offers": {"@type": "Offer", "price": "99.00", "priceCurrency": "AUD",
               "itemCondition": "https://schema.org/NewCondition",
               "seller": {"@type": "Person", "name": "auseller"}},
   "category": "Parts"
  },
  {"@type": "VideoObject"}
]
</script>
</head>
<body></body>
</html>
"""


@respx.mock
def test_scrape_item_returns_product_data():
    respx.get(HOMEPAGE_URL).mock(return_value=httpx.Response(200, text="<html></html>"))
    respx.get(ITEM_URL).mock(return_value=httpx.Response(200, text=SAMPLE_HTML))
    product = scrape_item(ITEM_URL)
    assert isinstance(product, ProductData)
    assert product.item_id == "123456789"
    assert product.title == "Genuine OEM Toyota Brake Pads"
    assert product.price == 45.99
    assert product.currency == "USD"
    assert product.condition == "New"
    assert product.seller_id == "autopartsking"
    assert product.category == "Auto Parts"
    assert product.item_url == ITEM_URL


@respx.mock
def test_scrape_item_image_urls_pipe_separated():
    respx.get(HOMEPAGE_URL).mock(return_value=httpx.Response(200, text="<html></html>"))
    respx.get(ITEM_URL).mock(return_value=httpx.Response(200, text=SAMPLE_HTML))
    product = scrape_item(ITEM_URL)
    urls = product.image_urls.split("|")
    assert len(urls) == 2
    assert all("ebayimg.com" in u for u in urls)


@respx.mock
def test_scrape_item_returns_none_on_404():
    url_404 = "https://www.ebay.com/itm/000000000"
    respx.get(HOMEPAGE_URL).mock(return_value=httpx.Response(200, text="<html></html>"))
    respx.get(url_404).mock(return_value=httpx.Response(404, text="Not Found"))
    result = scrape_item(url_404)
    assert result is None


@respx.mock
def test_scrape_item_item_specifics_json():
    respx.get(HOMEPAGE_URL).mock(return_value=httpx.Response(200, text="<html></html>"))
    respx.get(ITEM_URL).mock(return_value=httpx.Response(200, text=SAMPLE_HTML))
    product = scrape_item(ITEM_URL)
    specifics = json.loads(product.item_specifics)
    assert specifics.get("Brand") == "Toyota"
    assert specifics.get("Part Number") == "04465-33130"


@respx.mock
def test_scrape_item_shipping_and_listing_type():
    respx.get(HOMEPAGE_URL).mock(return_value=httpx.Response(200, text="<html></html>"))
    respx.get(ITEM_URL).mock(return_value=httpx.Response(200, text=SAMPLE_HTML))
    product = scrape_item(ITEM_URL)
    assert "free" in product.shipping.lower()
    assert product.listing_type == "Buy It Now"


@respx.mock
def test_scrape_item_handles_jsonld_array():
    """JSON-LD on ebay.com.au wraps product data in an array, not a top-level dict."""
    au_url = "https://www.ebay.com.au/itm/399000000001"
    au_home = "https://www.ebay.com.au/"
    respx.get(au_home).mock(return_value=httpx.Response(200, text="<html></html>"))
    respx.get(au_url).mock(return_value=httpx.Response(200, text=AU_ARRAY_HTML))
    product = scrape_item(au_url)
    assert product is not None
    assert product.title == "AU Product"
    assert product.currency == "AUD"
    assert product.item_id == "399000000001"

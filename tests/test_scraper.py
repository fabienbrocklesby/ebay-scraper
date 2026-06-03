import pytest
import respx
import httpx
from scraper.scraper import scrape_item, ProductData

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


@respx.mock
def test_scrape_item_returns_product_data():
    respx.get("https://www.ebay.com/itm/123456789").mock(
        return_value=httpx.Response(200, text=SAMPLE_HTML)
    )
    product = scrape_item("123456789")
    assert isinstance(product, ProductData)
    assert product.item_id == "123456789"
    assert product.title == "Genuine OEM Toyota Brake Pads"
    assert product.price == 45.99
    assert product.currency == "USD"
    assert product.condition == "New"
    assert product.seller_id == "autopartsking"
    assert product.category == "Auto Parts"


@respx.mock
def test_scrape_item_image_urls_pipe_separated():
    respx.get("https://www.ebay.com/itm/999999999").mock(
        return_value=httpx.Response(200, text=SAMPLE_HTML)
    )
    product = scrape_item("999999999")
    urls = product.image_urls.split("|")
    assert len(urls) == 2
    assert all("ebayimg.com" in u for u in urls)


@respx.mock
def test_scrape_item_returns_none_on_404():
    respx.get("https://www.ebay.com/itm/000000000").mock(
        return_value=httpx.Response(404, text="Not Found")
    )
    result = scrape_item("000000000")
    assert result is None


@respx.mock
def test_scrape_item_item_specifics_json():
    respx.get("https://www.ebay.com/itm/123456789").mock(
        return_value=httpx.Response(200, text=SAMPLE_HTML)
    )
    import json
    product = scrape_item("123456789")
    specifics = json.loads(product.item_specifics)
    assert specifics.get("Brand") == "Toyota"
    assert specifics.get("Part Number") == "04465-33130"


@respx.mock
def test_scrape_item_shipping_and_listing_type():
    respx.get("https://www.ebay.com/itm/123456789").mock(
        return_value=httpx.Response(200, text=SAMPLE_HTML)
    )
    product = scrape_item("123456789")
    assert "free" in product.shipping.lower()
    assert product.listing_type == "Buy It Now"

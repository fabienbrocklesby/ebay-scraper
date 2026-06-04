import json
import pytest
import respx
import httpx
from scraper.fetch import ChallengeError, WrongCountryError
from scraper.scraper import scrape_item, ProductData

ITEM_URL = "https://www.ebay.com/itm/123456789"
HOMEPAGE_URL = "https://www.ebay.com/"
DESC_URL = "https://itm.ebaydesc.com/itmdesc/123456789"
DESC_HTML = "<html><body>Brand new genuine brake pads. Fits all models.</body></html>"

SAMPLE_HTML = """
<html>
<head>
<script type="application/ld+json">
{
  "@type": "Product",
  "name": "Genuine OEM Toyota Brake Pads",
  "description": "",
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
<iframe src="https://itm.ebaydesc.com/itmdesc/123456789"></iframe>
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
   "description": "",
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
<body><iframe src="https://itm.ebaydesc.com/itmdesc/399000000001"></iframe></body>
</html>
"""


@respx.mock
def test_scrape_item_returns_product_data():
    respx.get(ITEM_URL).mock(return_value=httpx.Response(200, text=SAMPLE_HTML))
    respx.get(DESC_URL).mock(return_value=httpx.Response(200, text=DESC_HTML))
    client = httpx.Client(follow_redirects=True)
    product = scrape_item(ITEM_URL, client=client)
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
def test_scrape_item_description_fetched_from_iframe():
    respx.get(ITEM_URL).mock(return_value=httpx.Response(200, text=SAMPLE_HTML))
    respx.get(DESC_URL).mock(return_value=httpx.Response(200, text=DESC_HTML))
    client = httpx.Client(follow_redirects=True)
    product = scrape_item(ITEM_URL, client=client)
    assert "genuine brake pads" in product.description.lower()


@respx.mock
def test_scrape_item_description_empty_on_iframe_failure():
    respx.get(ITEM_URL).mock(return_value=httpx.Response(200, text=SAMPLE_HTML))
    respx.get(DESC_URL).mock(return_value=httpx.Response(503, text=""))
    client = httpx.Client(follow_redirects=True)
    product = scrape_item(ITEM_URL, client=client)
    assert product is not None
    assert product.description == ""


@respx.mock
def test_scrape_item_image_urls_pipe_separated():
    respx.get(ITEM_URL).mock(return_value=httpx.Response(200, text=SAMPLE_HTML))
    respx.get(DESC_URL).mock(return_value=httpx.Response(200, text=DESC_HTML))
    client = httpx.Client(follow_redirects=True)
    product = scrape_item(ITEM_URL, client=client)
    urls = product.image_urls.split("|")
    assert len(urls) == 2
    assert all("ebayimg.com" in u for u in urls)


@respx.mock
def test_scrape_item_returns_none_on_404():
    url_404 = "https://www.ebay.com/itm/000000000"
    respx.get(url_404).mock(return_value=httpx.Response(404, text="Not Found"))
    client = httpx.Client(follow_redirects=True)
    result = scrape_item(url_404, client=client)
    assert result is None


@respx.mock
def test_scrape_item_item_specifics_json():
    respx.get(ITEM_URL).mock(return_value=httpx.Response(200, text=SAMPLE_HTML))
    respx.get(DESC_URL).mock(return_value=httpx.Response(200, text=DESC_HTML))
    client = httpx.Client(follow_redirects=True)
    product = scrape_item(ITEM_URL, client=client)
    specifics = json.loads(product.item_specifics)
    assert specifics.get("Brand") == "Toyota"
    assert specifics.get("Part Number") == "04465-33130"


@respx.mock
def test_scrape_item_shipping_and_listing_type():
    respx.get(ITEM_URL).mock(return_value=httpx.Response(200, text=SAMPLE_HTML))
    respx.get(DESC_URL).mock(return_value=httpx.Response(200, text=DESC_HTML))
    client = httpx.Client(follow_redirects=True)
    product = scrape_item(ITEM_URL, client=client)
    assert "free" in product.shipping.lower()
    assert product.listing_type == "Buy It Now"


@respx.mock
def test_scrape_item_raises_on_challenge_not_silent_none():
    """A bot-challenge page (HTTP 200) must raise so rq retries, not drop the item."""
    challenge_html = "<html><head><title>Security Measure | eBay</title></head></html>"
    respx.get(ITEM_URL).mock(return_value=httpx.Response(200, text=challenge_html))
    client = httpx.Client(follow_redirects=True)
    with pytest.raises(ChallengeError):
        scrape_item(ITEM_URL, client=client)


@respx.mock
def test_scrape_item_cleans_entities_and_tags_in_title():
    html = SAMPLE_HTML.replace(
        "Genuine OEM Toyota Brake Pads", "Brake Pads 3/8&#034; Show<wbr>room 50&#039;"
    )
    respx.get(ITEM_URL).mock(return_value=httpx.Response(200, text=html))
    respx.get(DESC_URL).mock(return_value=httpx.Response(200, text=DESC_HTML))
    client = httpx.Client(follow_redirects=True)
    product = scrape_item(ITEM_URL, client=client)
    assert product.title == "Brake Pads 3/8\" Showroom 50'"


@respx.mock
def test_scrape_item_raises_on_wrong_country_currency():
    """A foreign currency (wrong-country exit IP) must retry, not store bad prices."""
    brl_html = SAMPLE_HTML.replace('"priceCurrency": "USD"', '"priceCurrency": "BRL"')
    respx.get(ITEM_URL).mock(return_value=httpx.Response(200, text=brl_html))
    respx.get(DESC_URL).mock(return_value=httpx.Response(200, text=DESC_HTML))
    client = httpx.Client(follow_redirects=True)
    with pytest.raises(WrongCountryError):
        scrape_item(ITEM_URL, proxy_url="http://u:p@geo.iproyal.com:12321", client=client)


@respx.mock
def test_scrape_item_allows_foreign_currency_without_proxy():
    """Without a proxy the local IP's currency is legitimate, so no retry is forced."""
    brl_html = SAMPLE_HTML.replace('"priceCurrency": "USD"', '"priceCurrency": "BRL"')
    respx.get(ITEM_URL).mock(return_value=httpx.Response(200, text=brl_html))
    respx.get(DESC_URL).mock(return_value=httpx.Response(200, text=DESC_HTML))
    client = httpx.Client(follow_redirects=True)
    product = scrape_item(ITEM_URL, client=client)
    assert product.currency == "BRL"


@respx.mock
def test_scrape_item_handles_jsonld_array():
    """JSON-LD on ebay.com.au wraps product data in an array, not a top-level dict."""
    au_url = "https://www.ebay.com.au/itm/399000000001"
    au_desc_url = "https://itm.ebaydesc.com/itmdesc/399000000001"
    respx.get(au_url).mock(return_value=httpx.Response(200, text=AU_ARRAY_HTML))
    respx.get(au_desc_url).mock(return_value=httpx.Response(200, text=DESC_HTML))
    client = httpx.Client(follow_redirects=True)
    product = scrape_item(au_url, client=client)
    assert product is not None
    assert product.title == "AU Product"
    assert product.currency == "AUD"
    assert product.item_id == "399000000001"

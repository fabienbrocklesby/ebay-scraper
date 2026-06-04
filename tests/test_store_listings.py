from pathlib import Path

import httpx
import respx

from scraper.store import get_store_listings, parse_listing_cards

FIXTURE = Path(__file__).parent / "fixtures" / "store_listing.html"


def test_parse_listing_cards_extracts_id_and_price():
    html = FIXTURE.read_text()
    cards = parse_listing_cards(html)
    assert len(cards) >= 180
    item_id, price, url = cards[0]
    assert item_id.isdigit() and 11 <= len(item_id) <= 13
    assert price is None or isinstance(price, float)
    assert "/itm/" in url
    assert len({c[0] for c in cards}) == len(cards)


ONE_CARD = '<div class="str-item-card"><a href="/itm/123456789012">x</a> 49.99</div>'
EMPTY = "<div>no cards</div>"


@respx.mock
def test_get_store_listings_paginates_until_no_cards():
    base = "https://www.ebay.com.au/str/x"
    respx.get(url__startswith=base).mock(
        side_effect=[
            httpx.Response(200, text=ONE_CARD),
            httpx.Response(200, text=EMPTY),
        ]
    )
    listings = get_store_listings(base, _session=httpx.Client())
    assert listings == [
        ("123456789012", 49.99, "https://www.ebay.com.au/itm/123456789012")
    ]

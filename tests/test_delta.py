from scraper.delta import compute_delta


def test_compute_delta_new_changed_missing():
    listings = [
        ("111111111111", 10.0),
        ("222222222222", 25.0),
        ("333333333333", 5.0),
    ]
    db_prices = {
        "111111111111": 10.0,
        "222222222222": 20.0,
        "444444444444": 8.0,
    }
    to_fetch, to_deactivate = compute_delta(listings, db_prices)
    assert sorted(to_fetch) == ["222222222222", "333333333333"]
    assert to_deactivate == ["444444444444"]


def test_compute_delta_none_price_is_not_a_change():
    listings = [("111111111111", None)]
    db_prices = {"111111111111": 10.0}
    to_fetch, to_deactivate = compute_delta(listings, db_prices)
    assert to_fetch == []
    assert to_deactivate == []

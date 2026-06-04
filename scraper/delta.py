# scraper/delta.py
"""Pure delta computation: compare current store listings against DB state."""


def compute_delta(
    listings: list[tuple[str, float | None]],
    db_prices: dict[str, float],
) -> tuple[list[str], list[str]]:
    """Return (item_ids_to_fetch, item_ids_to_deactivate).

    Fetch: items not in the DB, or whose listing price differs from the stored
    price. A None listing price means "price not shown on the card" and is never
    treated as a change. Deactivate: DB items no longer present in the store.
    """
    to_fetch: list[str] = []
    listing_ids: set[str] = set()
    for item_id, price in listings:
        listing_ids.add(item_id)
        if item_id not in db_prices:
            to_fetch.append(item_id)
        elif price is not None and abs(price - db_prices[item_id]) > 0.001:
            to_fetch.append(item_id)
    to_deactivate = [item_id for item_id in db_prices if item_id not in listing_ids]
    return to_fetch, to_deactivate

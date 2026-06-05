# The "hard ~20%" is a marketplace mismatch, not anti-bot

Date: 2026-06-05
Status: root cause identified and the free fix is proven on live infra. Detection mechanism is the remaining build.

## TL;DR

The stores that returned a fake "0 results" page were not being blocked. They are
sellers whose home eBay marketplace is **not** `ebay.com` (e.g. Australian sellers on
`ebay.com.au`). eBay only shows a seller's full item grid on their home domain to an
in-country viewer. We were crawling every seller on `ebay.com` from US IPs, so non-US
sellers came back with a genuine "0 results".

Routing a seller to their home domain fixes it. The scraper's existing
`apply_proxy_country` (`scraper/fetch.py`) then auto-pins the IPRoyal exit IP to that
domain's country. No unblocker API is needed for this class of failure.

## Evidence (live, 2026-06-05)

Two known-hard stores from `failed_stores.txt`: `goodpricepeople`, `af2018`. Both are
high-volume Australian sellers (goodpricepeople: 361k+ AU sales).

Oxylabs `source: ebay` (rules out our parser / our IP reputation):

| Target | geo | items extracted |
|---|---|---|
| `ebay.com/sch?_ssn=goodpricepeople` | United States | 0 ("0 results") |
| `ebay.com/sch?_ssn=goodpricepeople` | Australia | 0 ("0 results") |
| **`ebay.com.au/sch?_ssn=goodpricepeople`** | **Australia** | **240** |
| `ebay.com.au/sch?_ssn=goodpricepeople` | United States | 0 |
| `ebay.com.au/sch?_ssn=af2018` | Australia | 240 |
| `ebay.com/sch?_ssn=af2018` | United States | 0 |

Both the rendered `srp-controls__count-heading` and the embedded JSON data model said
"0 results" on `ebay.com`, so it is the real server response, not a render/parse miss.
Needs **both** the right domain **and** an in-country IP: `.com.au` + US geo = 0.

Controls (easy stores, same Oxylabs path): `onlinesound` -> 200 items, `tool007tool`
-> 202 items. Confirms the parser matches Oxylabs output and Oxylabs returns real grids.

## The free fix is proven (no Oxylabs)

Through the production IPRoyal proxy (`geo.iproyal.com:12321`), using the scraper's own
`build_session` + `apply_proxy_country` (which injected `_country-au`):

- `ebay.com.au/sch?_ssn=goodpricepeople` (AU exit) -> **240 items**, no challenge
- `ebay.com.au/sch?_ssn=af2018` (AU exit) -> **240 items**, no challenge

So the lever is domain routing, which the existing country-pinning code already
supports. Oxylabs is not required for marketplace-mismatch stores.

(The `ebay.com` US-exit attempt returned a "Pardon Our Interruption" challenge, not
"0 results" - that is the separate, already-known single-IP reputation ceiling from
heavy testing, not this bug.)

## Why prior proxy testing missed it

Earlier testing varied only the residential IP pool (IPRoyal vs Decodo), never the eBay
**domain**. Every test hit `ebay.com`. "Switching pools doesn't fix it" is fully
consistent with "wrong domain", which is what this is.

## Scope / honesty

n = 2 known-hard stores, both Australian. This proves marketplace mismatch is *a* (and
for both known stores, *the*) cause. It does not prove all of the measured ~20% are
marketplace mismatches - some residual could be genuine anti-bot or other causes. The
fix must degrade gracefully when no probed marketplace returns items.

## Remaining build: home-marketplace detection

The wrong-marketplace `ebay.com` page does **not** leak the seller's home domain, and
the `/usr/` profile page on `.com` shows only a shipping-region dropdown, no clean
single-country signal. So detection needs an active probe:

1. Crawl on `ebay.com` as today.
2. If a store returns a genuine 0-results (not a challenge), probe the seller-search
   across a small ordered marketplace list (`.com.au`, `.co.uk`, `.de`, `.ca`, ...) and
   adopt the first domain that returns items.
3. Persist the resolved `(domain, country)` on the store row so future `scrape start` /
   `scrape delta` runs go straight to the right marketplace (one-time detection cost).
4. Order the probe list by Kieran's catalogue skew (likely AU/UK-heavy). If his list is
   mostly AU, consider making `.com.au` the default crawl domain and inverting the probe.

This ties to the open "total catalogue size" decision: the marketplace mix sizes both
the default domain and the probe order.

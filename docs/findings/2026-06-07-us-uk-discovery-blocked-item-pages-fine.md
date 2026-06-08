# Discovery must use /str storefront pages, not /sch seller-search (and Oxylabs is not needed)

**Date:** 2026-06-07 (updated 2026-06-08 with the storefront finding)
**Context:** Standing up Kieran's first real run (112 stores, mostly US sellers) on the
supplied IPRoyal residential account. Validated live against eBay before provisioning.

## What is blocked, what is not (all through the IPRoyal residential proxy)

| Surface | US | UK | AU |
|---|---|---|---|
| `/sch/i.html?_ssn=` seller-search | blocked 0/8 | blocked 0/8 | ok 7/8 |
| `/str/<store>` storefront | **ok** | ok | ok |
| `/itm/<id>` item detail | **ok 10/10** | - | ok 9/10 |

eBay defends the **seller-search** endpoint hard and challenges any residential proxy on
it (confirmed on both IPRoyal and Oxylabs residential pools). It does **not** defend the
**storefront** (`/str/<store>`) or **item-detail** pages the same way: those load fine on
the cheap IPRoyal proxy. The whole pipeline therefore uses storefront for discovery and
item pages for detail, and never touches seller-search.

## Consequences for the build

1. **Discovery is storefront-based.** `marketplace.detect_marketplace` and the crawl probe
   `/str/<store_slug>`, not `/sch/?_ssn=`. The store list is fed as `/str/<store_name>`
   URLs (every store in Kieran's sheet has a store name). `_seller_search_url` and
   `_marketplace_seller_search` build storefront URLs despite their legacy names.

2. **Oxylabs is not needed for the normal batch.** Storefront discovery + item detail both
   run on IPRoyal. This overturns the earlier "Oxylabs required" note (that was concluded
   before the storefront finding). The Oxylabs unblocker remains wired as an optional tail
   fallback (`get_item_urls_via_unblocker`, `_scrape_one_with_unblocker`) for any residual
   store that genuinely cannot be read, but it is off by default and unused here.

3. **Cross-border listings break naive home detection.** eBay surfaces a seller's items on
   many marketplaces, each capped at one ~200-item page:

   | Seller | .com | .com.au | .co.uk | .de | .ca |
   |---|---|---|---|---|---|
   | autop76 (US) | 205 | 0 | 0 | - | 202 |
   | au2parts2014 (AU) | 0 | 200 | 200 | 0 | 200 |
   | dieseltechnikbiberach (DE) | 3 | 200 | 200 | 200 | 200 |

   Page-1 item count cannot tell a home catalogue from a cross-listing (both fill a page).
   Detection therefore picks the domain with the highest page-1 count and breaks ties by
   candidate priority order (`.com -> .com.au -> .co.uk -> .de -> .ca`). This resolves the
   US and AU majority correctly. A genuinely foreign seller heavily cross-listed onto an
   earlier-priority domain (e.g. the one German store) can resolve to a cross-listing
   subset; that is an accepted small tail, triaged manually, not solved tonight.

4. **Currency is recorded per item, so detection is about completeness, not currency.**
   Each item page carries its own `priceCurrency` (AUD on .com.au, USD on .com), captured
   into the CSV `currency` column. Kieran already splits exports by currency (see HANDOFF).
   So picking a domain is about scraping a seller's full catalogue, not about guaranteeing
   one currency. Verified live: an au2parts2014 item fetched from its resolved `.com.au`
   home returns AUD.

## Cost shape (unchanged conclusion, better number)

- Item bulk: IPRoyal, ~135 KB/item, ~70 GB per ~494k items. The only real spend.
- Discovery: IPRoyal storefront pages, free-tier of the same proxy. No Oxylabs spend.

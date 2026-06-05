# Live validation run — marketplace-aware scraping

Date: 2026-06-05
Branch: feat/marketplace-aware-scraping (122 unit tests passing)

## Summary

The marketplace-mismatch fix is correct and proven: the known-hard AU stores resolve to
`ebay.com.au` automatically. Live validation also exposed a **separate, pre-existing
problem — the IP-reputation ceiling — now showing up country-specific**, and a detection
honesty bug, which has been fixed. These are two different problems; do not conflate them.

## Problem A — routing correctness (FIXED in code)

`detect_marketplace` originally adopted whichever marketplace returned items. Under a
proxy whose US/GB exits are challenge-flagged (see Problem B), a US seller's `ebay.com`
probe is challenged while the seller's cross-listing on `ebay.com.au` answers, so the old
code **silently routed a US seller to `ebay.com.au`** — wrong currency (AUD), possibly a
partial catalogue. For a years-unattended Shopify feed, silently-wrong data is the worst
failure mode.

**Fix (commit on this branch):** detection is now honest. A proxy result is adopted only
when every candidate domain answered conclusively (a result AND zero undetermined/
challenged domains). If any candidate is undetermined, the proxy result is never silently
adopted; the system escalates to an authoritative unblocker detection (clean probes on all
marketplaces, item counts differentiate true home from cross-listing) when the unblocker
is configured, otherwise leaves the store **unresolved and loud** (-> `failed_stores`)
rather than scraping it in the wrong currency. Detection page size raised 60 -> 240 so the
"most items wins" rule can actually distinguish a home (hundreds of items) from a thin
cross-listing.

Verified with the Oxylabs unblocker as the authoritative detector: `onlinesound` ->
`ebay.com` (US), `goodpricepeople` / `af2018` -> `ebay.com.au` (AU), all with zero
undetermined domains. Correct routing for all three.

## Problem B — IP reputation (NOT a code problem; needs an infra decision)

Through the current IPRoyal proxy, the **US and GB residential exits are challenge-flagged
by eBay** ("Pardon Our Interruption") on every retry, while the **AU exit is clean**:

- `ebay.com` probe (US exit): challenged, 3/3 retries
- `ebay.co.uk` probe (GB exit): challenged, 3/3 retries
- `ebay.com.au` probe (AU exit): clean, 186 items

This is the IP-reputation ceiling CLAUDE.md already documents ("one IP challenges after
~3,500 fast requests"), now country-specific. It is most likely this **IPRoyal test
account being hammered** across this and prior sessions (heavy US testing), not a
store-specific or code issue. It cannot be resolved in code or confirmed transient-vs-
persistent from here.

Critically: **no detection change fixes Problem B.** Routing a US seller correctly to
`ebay.com` still means the millions of `ebay.com` item pages get bulk-scraped through the
same flagged US exits -> challenge storms, now per-item -> the per-item unblocker
escalation fires -> expensive. Correct routing is cheap; reliable US/UK *bulk throughput*
is the thing that gates the >90% goal, and it depends on a non-flagged residential pool.

## What this means for the >90%-across-US/AU/UK goal

- Code is complete and AU is proven end to end.
- US/UK bulk scraping clearing >90% is **gated on proxy reputation**, currently failing on
  this test account's US/GB exits.

## Decision sequence for Fabien

1. **First, infra not code: retest US/UK through a fresh proxy account (Kieran's own, not
   this hammered test account).** A burned test account would explain everything and may
   dissolve Problem B for production. Cheapest possible fix.
2. If a fresh pool still walls US/GB, then the cost decision is real:
   - Detection-via-unblocker is ~3 requests/store one-time, ~**$6 across 1500 stores** —
     trivial, buys correct routing regardless of proxy health.
   - Bulk US/UK via unblocker on **millions** of item pages is **$thousands** — so total
     catalogue size (open decision #2) becomes a hard cost driver, and the lever becomes
     "fix/replace the residential pool" rather than "pay the unblocker for everything."

## Live re-validation of the integrated honest path

`_discover_store` run end to end against the flagged proxy, unblocker OFF then ON:

| Store | OFF (no unblocker) | ON (unblocker for detection) |
|---|---|---|
| onlinesound (US) | empty, unresolved (loud) | **ok, ebay.com, 230 items** |
| goodpricepeople (AU) | empty, unresolved (loud) | **ok, ebay.com.au, 3,829 items** |
| af2018 (AU) | empty, unresolved (loud) | **ok, ebay.com.au, 405 items** |

Two things this proves:

1. **The honesty fix works.** With no unblocker and a degraded proxy, every store whose
   home could not be confidently determined comes back unresolved/empty (reported loudly
   into `failed_stores`), never silently scraped in the wrong currency.

2. **The practical architecture works today, even on the hammered account.** With the
   unblocker used only for the one-time routing decision, all three stores resolved to the
   correct marketplace AND their bulk item grids were then pulled **through the cheap
   proxy** — including the US store (`onlinesound` -> 230 items via `ebay.com`). The bulk
   crawler's session rotation punches through the intermittent US-exit challenges that
   block a single detection probe. So Problem B is real but **less severe for bulk
   throughput than the single-probe challenges suggested**: routing is the fragile step,
   not the crawl.

### Revised practical picture

- **Working path now:** unblocker for detection (only fires when the proxy can't give a
  clean read; ~3 requests/store one-time, ~$6 across 1500 stores) + proxy for bulk. This
  resolved and scraped all three marketplaces correctly on a degraded test proxy.
- **A fresh/healthy proxy account** would make detection confident on the proxy itself, so
  the unblocker would rarely fire and detection would be free too. Still the recommended
  first step, but it is an optimisation, not a prerequisite, for correct multi-marketplace
  scraping.
- Sustained million-item throughput on US/UK still rests on residential-pool reputation
  (add boxes / modest RPS), unchanged by this work. One store at 230 items is not a
  load test.

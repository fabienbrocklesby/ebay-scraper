# eBay Scraper - Locked Strategy (read this before changing anything)

This file exists so we stop re-deriving the same conclusions. These are settled,
empirically verified facts. Do not re-run experiments to "check" them. If you think
one is wrong, you need new primary evidence, not a hunch.

## The one model that matters: two surfaces, two proxies

eBay treats two request types completely differently:

| Surface | What it is | Reputation cost | Proxy to use |
|---|---|---|---|
| **Discovery** | Paging a store's listing grid (`/str/<seller>?_pgn=N`) | **High** - eBay challenges this fast | **Rotating residential** (fresh exit IP per page) |
| **Item fetch** | A single item page (`/itm/<id>`) | **Low** - tolerant at gentle rates | **Static ISP pool** (flat-rate, cheap) |

- **Item fetching does not burn IPs at a gentle rate** (~1 req/sec/IP). Proven: clean 10/10 and 5/5 item pulls through fresh ISP IPs.
- **Discovery/pagination burns a single IP quickly.** A static IP died after ~1,500 fast pagination requests (~6 min). That is why discovery must run on rotating residential, where every page gets a fresh exit IP and no single IP accumulates load.
- **Never route discovery/pagination through the static ISP pool.** It burns the IPs you need for fetching. (`--discover-via-pool` exists only as an emergency lever when residential is unavailable, and it WILL wear the pool. Default off. Avoid.)

## Settled facts (do not re-test)

1. **eBay caps store browse at ~10,000 items/store.** No scraper can pull all items of a 100k+ store via the public storefront. Price-partitioning extends this somewhat (tens of thousands) but not to millions. "Scrape every store in full" is true for the many small stores; the giants hit eBay's ceiling. This is eBay's limit, not ours.
2. **Marketplace mismatch is real.** A seller's full grid only shows on their home marketplace (US sellers on ebay.com, AU on ebay.com.au, etc.) viewed from an in-country IP. With a **US-only ISP pool we scrape US sellers only**; run with `--us-only` so non-US sellers return empty at discovery instead of burning reputation on doomed wrong-country fetches. Non-US sellers are a deferred tail (would need in-country ISP IPs).
3. **Datacenter/VPS IPs get a hard 403 from eBay on item pages.** The VPS only orchestrates; all eBay traffic egresses through the proxies, never the VPS IP.
4. **Bot-challenge pages return HTTP 200** (titles "Security Measure", "Pardon Our Interruption"). They are detected explicitly and never treated as "no more data" (that silently truncates crawls).
5. **A burned IP cools slowly** (hours, not minutes). Do not hammer to "check" - that resets the clock. Let it rest.
6. **Repeatedly re-running full discovery degrades the residential pool.** Discover each store ONCE, then use `scrape delta` for updates. The throughput crisis on 2026-06-08 was caused by ~10 full re-runs in an hour while debugging, not by the normal workload.

## Operating rules (the locked playbook)

- **DO** run discovery on rotating residential, fetching on the static ISP pool.
- **DO** run `scrape start --us-only --cap-per-store 5000` for a first pass; `scrape retry --us-only --cap-per-store 5000` to mop up blocked stores as IPs cool.
- **DO** scale throughput by adding ISP IPs (`scraper proxy pool add`). It is live - the next batch uses them, no restart, no lost progress.
- **DO** keep `MAX_RPS_PER_IP` around 1.0 (gentle). Item fetch tolerates it; higher risks burning.
- **DON'T** route discovery through the ISP pool (`--discover-via-pool`) except as a last resort.
- **DON'T** re-run full discovery repeatedly. Once is enough; the queue + dedup set handle the rest.
- **DON'T** crank rates to go faster. Add IPs instead.

## Throughput math (use for honest estimates)

Fetching is the bottleneck and it is linear in IP count at a safe ~1 item/sec/IP:

| ISP IPs | ~items/sec | ~items/day |
|---|---|---|
| 5 | 5 | ~430,000 |
| 10 | 10 | ~860,000 |
| 12 | 12 | ~1,000,000 |
| 15 | 15 | ~1,300,000 |

So **a million products/day needs ~12-15 ISP IPs**, not 5. Five IPs is ~400k/day.
Discovery is a tiny fraction of requests (1 page per 240 items) and is not the limit
once it is running. The live `scraper scrape monitor` shows the real measured rate and
projects items/day from it - trust that number over the table once data is flowing.

## Cost

- **Item fetch: free bandwidth** (flat-rate static ISP IPs). This is the whole cost win vs per-GB rotating residential.
- **Discovery residential: ~$15-45** for a full pass (storefront pages are ~2 MB each; ~10 KB/item on the wire).
- Total for the full catalogue: well under $100, vs the thousands that pure rotating-residential fetching would have cost.

## How it runs in production (the VPS)

- Coordinator (Redis + Postgres) + worker + discovery loop all run on one VPS via systemd (`deploy/ebay-worker.service`, `deploy/ebay-discovery.service`). Both `Restart=always`, so scraping **auto-resumes** after crashes, reboots, or once burned proxies cool.
- `scraper scrape monitor` - live progress dashboard.
- `scraper scrape status` - one-shot progress numbers.
- `scraper proxy pool add <host:port:user:pass>` - add an ISP IP live to speed things up.
- `scraper export --output products.csv` - export.

See `README.md` for the full command reference and `HANDOFF.md` for from-scratch setup.

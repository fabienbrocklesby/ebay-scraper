# eBay Scraper, Setup and Operating Guide

This is the complete, tested guide to running the scraper. Every command here was
run end to end against live eBay on 2026-06-04 (coordinator on a Mac, worker on a
real VPS, through a residential proxy, on real stores). Follow it top to bottom.

Repo: **https://github.com/fabienbrocklesby/ebay-scraper** (private)

---

## 1. How it works (read this first, it is short)

- You run **one coordinator** (your own PC). It holds the job queue (Redis) and
  the results database (Postgres), and it is where you type commands (add stores,
  start a scrape, export the CSV).
- You run **as many worker VPSs as you want**. Each worker pulls jobs from the
  coordinator's queue and does the actual page scraping. They all share one queue,
  so **adding a worker just makes it faster**, with zero extra configuration. Work
  is split across workers automatically; no two workers do the same item.
- Workers reach the coordinator over **Tailscale** (a free private network), so you
  never expose your PC's database to the internet.
- eBay blocks data-centre/VPS IPs, so workers fetch through a **rotating
  residential proxy** (IPRoyal). The proxy is set once on the coordinator and every
  worker uses it automatically.

```
   YOUR PC (coordinator)                 VPS workers (add as many as you want)
   - Redis (job queue)      <--- Tailscale --->   worker 1  ─┐
   - Postgres (results)                            worker 2  ─┼─ all pull from the
   - you type commands                             worker N  ─┘   one shared queue
        |                                                |
        |  you: store add / scrape start / export        |  each: scrape pages
        v                                                v   via the residential proxy
   products.csv  <-------------------------------  written to Postgres
```

**The data you get per item:** `item_id, title, price, currency, condition,
description, image_urls (full gallery, full resolution), item_url, seller_id,
store_url, category, item_specifics, mpn, upc, shipping, listing_type, niche,
scraped_at`. Exactly what you need to import into Shopify.

---

## 2. Before you start: three things you need

1. A **GitHub access** to the private repo. Two options:
   - **Easiest:** make the repo public (GitHub > repo > Settings > General >
     Change visibility). Then every `git clone` below works with no token.
   - **Keep it private:** create a read-only token at
     <https://github.com/settings/tokens> (fine-grained, read-only, this repo only),
     and in the `git clone` commands replace the URL with:
     `https://x-access-token:YOUR_TOKEN@github.com/fabienbrocklesby/ebay-scraper.git`
2. A **Tailscale account** (free): <https://tailscale.com>. Install it on the
   coordinator and on each VPS, all signed into the same account.
3. An **IPRoyal residential proxy** with **bandwidth/credit on it**:
   <https://iproyal.com> > Residential. Your proxy URL looks like
   `http://USERNAME:PASSWORD@geo.iproyal.com:12321`. Budget ~1 GB per ~7,000 items
   (see section 7). **If the proxy runs out of credit, scraping silently fails with
   a "402" error, keep credit on it.**

Placeholders used below (fill in your own values):
- `<COORDINATOR_IP>`  = your PC's Tailscale IP (a `100.x.y.z` address)
- `<PROXY_URL>`       = `http://USERNAME:PASSWORD@geo.iproyal.com:12321`

---

## 2b. Shopping list: what to buy and where

**Coordinator:** your own PC. Costs nothing. Keep it on while scraping.

**Worker VPSs** (rent as many as you want, more = faster). Any cheap Linux VPS:
- **Hetzner Cloud** (cheapest, recommended): <https://www.hetzner.com/cloud> — a CPX11
  (2 vCPU, 2 GB) is about EUR 4-5/month, or pennies/hour billed hourly. Ubuntu 22.04.
- **Vultr** (<https://www.vultr.com>) or **DigitalOcean** (<https://www.digitalocean.com>):
  ~$6/month equivalents, hourly billing. Pick the cheapest 1-2 vCPU Ubuntu instance.
- One VPS does ~2 items/sec (~190k items/day). Rent N of them for N times the speed;
  they share the queue automatically. For a big one-off run, rent several by the hour
  and delete them when done.

**Proxy** (the workers fetch through this). One account, billed by bandwidth (per GB):
- **IPRoyal Residential** (<https://iproyal.com>, what this was built and tested on):
  ~$2-7/GB. Endpoint `geo.iproyal.com:12321`. Pay-as-you-go, buy a bulk GB package for
  big runs. Budget ~1 GB per ~7,000 items.
- **Decodo / Smartproxy** (<https://decodo.com>): cheaper (~$2.2/GB), works about the
  same. Use country subdomains, e.g. `us.decodo.com:10001`. A drop-in alternative.
- **Honest note (read section 10):** any rotating residential proxy reliably gets ~80%
  of stores. A stubborn ~15-20% serve a fake "0 results" page to *all* residential
  proxies; those are recovered by the coordinator's own IP + `scraper scrape retry`,
  or, for hands-off near-100%, by an **unblocker API** (Bright Data Web Unlocker
  ~$1.30/1,000 requests, or Oxylabs eBay Scraper API). Start with a plain residential
  proxy; only add an unblocker if the re-run list stays too big.

**Tailscale:** free (<https://tailscale.com>), connects the coordinator and VPSs privately.

---

## 3. Coordinator setup (your PC, one time)

Tested end to end on **macOS and Linux**. Windows should work with Docker Desktop,
Python, git and Tailscale installed (run the same `scraper ...` commands in
PowerShell), but it was not tested, so prefer macOS or Linux for the coordinator if
you can. You need **Docker**, **Python 3.11+ (with pip)**, **git**, and
**Tailscale** installed first. On a fresh Linux box:
`apt update && apt install -y python3-pip git docker.io`.

```bash
# 1. Install Tailscale and sign in, then confirm you have an IP:
tailscale ip -4            # note this address, it is your <COORDINATOR_IP>

# 2. Get the code and the CLI
git clone https://github.com/fabienbrocklesby/ebay-scraper.git
cd ebay-scraper
python3 -m pip install -e .            # installs the `scraper` command
#   (Linux may need:  python3 -m pip install -e . --break-system-packages)

# 3. Start the coordinator services (Redis + Postgres in Docker) and the database
scraper coordinator start
scraper connect localhost
scraper db init

# 4. Set the proxy once (every worker picks it up automatically) and test it
scraper proxy set "<PROXY_URL>"
scraper proxy test                     # should print: OK  Proxy working. Scraped: ...
```

The coordinator is ready. Keep this machine on while scraping; the workers need
its Redis and Postgres.

---

## 4. Worker VPS setup (repeat for each worker)

Any cheap Linux VPS (Ubuntu 22.04 / Debian 12). One worker ≈ one IP's worth of
speed; add more for more speed. SSH in as root, then:

```bash
# 1. Tailscale (same account as the coordinator)
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up                           # open the printed URL once to authorise

# 2. Tools + the code + the CLI
apt update && apt install -y python3-pip git docker.io
git clone https://github.com/fabienbrocklesby/ebay-scraper.git /root/ebay-scraper
cd /root/ebay-scraper
python3 -m pip install -e . --break-system-packages

# 3. One command sets up and starts the worker (builds the Docker image and runs it)
scraper init <COORDINATOR_IP> --proxy "<PROXY_URL>"
```

That is it. `scraper init` writes the config, builds the worker image, and starts
the worker container with `--restart=always` (survives reboots). The worker is now
pulling jobs. Repeat on every VPS; they all share the one queue and the one proxy.

Confirm from the **coordinator**:
```bash
scraper scrape status                  # workers show up; queued jobs fall as they work
```

---

## 5. Daily use (all on the coordinator)

### Add stores (two ways, your choice)

```bash
# one at a time
scraper store add https://www.ebay.com/str/STORENAME --niche tools

# or bulk, from a text file (one store per line: "URL" or "URL,niche")
scraper store import stores.txt --niche tools
scraper store list
```

`stores.txt` format (blank lines and `#` comments ignored; a line's own niche
wins, `--niche` is the default for lines without one):
```
# my stores
https://www.ebay.com/str/store-one,tools
https://www.ebay.com/str/store-two,sound
https://www.ebay.com/sch/i.html?_ssn=SELLERNAME      # a seller-search URL also works
```
Both `/str/STORENAME` store URLs and `/sch/i.html?_ssn=SELLERNAME` seller-search
URLs work. Use the seller-search form if a store's page does not list items (some
sellers' store name differs from their username).

### Scrape, check, export

```bash
scraper scrape start                   # crawl every registered store, queue all items
scraper scrape start --niche tools     # or just one niche
scraper scrape retry                   # re-run only the stores that returned 0 last time
scraper scrape status                  # progress: queued jobs + totals
scraper export --output products.csv   # write the CSV (everything)
scraper export --niche tools --output tools.csv
```

`scrape start` reports every store as **OK (N items)**, **0 results**, or **BLOCKED**,
and saves anything that returned nothing to `~/.config/ebay-scraper/failed_stores.txt`.
A "0 results" store is usually a flagged-IP fake-empty, not a truly empty store (see
section 10). Just run **`scraper scrape retry`** a bit later (fresh IPs recover most of
them); repeat until the list is small. This is normal at scale, not a failure.

### Keep it fresh cheaply (after the first full scrape)

```bash
scraper scrape delta                   # re-checks listings, only re-scrapes new/changed
```
Run `scrape start` once per store to build the catalogue, then `scrape delta` on a
schedule (e.g. a daily cron) to keep prices and new items current for almost no cost.

### Tidy up

```bash
scraper scrape stop                    # cancel pending jobs
scraper clear --niche tools            # delete scraped rows for a niche
scraper store remove <url>             # unregister a store
```

---

## 6. A 10-minute demo you can run right now

These three real stores were used to validate the tool (one of them is a
seller-search URL):

```bash
scraper store add https://www.ebay.com/str/tool007tool --niche tools
scraper store add https://www.ebay.com/str/onlinesound --niche sound
scraper store add "https://www.ebay.com/sch/i.html?_ssn=redtiger_store" --niche tech
scraper scrape start
# wait a couple of minutes, watch it work:
scraper scrape status
scraper export --output demo.csv
```
Open `demo.csv`: each row has title, price, the **full image gallery** (full-res
URLs, pipe-separated), the description, and the item URL.

---

## 7. Speed and cost (real measured numbers)

- **Throughput:** ~2.2 items/sec per VPS at the default settings, through the proxy.
  You can push a single VPS higher by raising its rate (the proxy rotates IPs, so
  there is no single-IP limit), and you add VPSs for linear speed. Rough planning:
  one default VPS does ~190k items/day; ten do ~1.9M/day.
- **Time estimate:** items to scrape ÷ (2.2 × number_of_VPSs) seconds. Example:
  100,000 items on 5 VPSs ≈ 100000 / (2.2 × 5) ≈ 2.5 hours.
- **Proxy cost:** ~135 KB on the wire per item, so about **1 GB per ~7,000 items**.
  At bulk IPRoyal rates (~$2/GB) that is roughly **$30 per 100,000 items**, or
  ~$1,400 for 5 million. Buy a bulk traffic package, not pay-as-you-go, for big runs.
- **Tuning per VPS** (set as environment variables, or edit the worker's
  `~/.config/ebay-scraper/.env` and `docker restart ebay-scraper-worker`):
  `WORKER_CONCURRENCY` (default 8, parallel fetches), `MAX_RPS_PER_IP` (default 6),
  `BATCH_SIZE` (default 200).

---

## 8. Updating to a new version

On the coordinator and on each VPS:
```bash
cd ebay-scraper            # or /root/ebay-scraper on a VPS
git pull
python3 -m pip install -e . --break-system-packages    # coordinator: drop the flag if not needed
```
On each VPS, also rebuild and restart the worker after pulling:
```bash
scraper init <COORDINATOR_IP> --proxy "<PROXY_URL>"     # rebuilds + restarts the worker
```

---

## 9. Troubleshooting

- **`proxy test` fails / jobs do nothing and logs show "402".** The IPRoyal account
  is out of bandwidth. Top it up.
- **A store scrapes 0 items.** That store may be empty/inactive, or its page is a
  storefront landing page. Try the seller-search URL form
  `https://www.ebay.com/sch/i.html?_ssn=SELLERNAME`.
- **`scrape start` says a store was BLOCKED.** eBay challenged the coordinator's IP
  during the crawl. Make sure the proxy is set (`scraper proxy set`) and re-run; the
  crawl routes through the proxy and retries. Already-queued items are not lost.
- **Worker not picking up jobs.** On the VPS, `docker logs ebay-scraper-worker` and
  check `tailscale status` lists the coordinator. On the coordinator,
  `scraper coordinator status` should show Redis and Postgres up.
- **macOS coordinator, worker run locally crashes with an `objc fork()` error.**
  Only affects running a worker directly on a Mac. Start it with
  `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES scraper worker start`. Production workers
  run on Linux and are unaffected.

---

## 10. Honest limits (so there are no surprises)

- **About 15-20% of stores serve a fake "0 results" page to residential proxies.**
  eBay detects proxy IPs and, for some stores, serves an empty storefront instead of
  the real item grid, to every proxy IP, on every provider (tested IPRoyal and Decodo,
  same result). It is not an empty store and not a bug. Those stores are recovered by:
  (a) the **coordinator's own home/office IP**, which the scraper automatically falls
  back to and which eBay serves the real grid; and (b) **`scraper scrape retry`**, which
  re-runs the leftovers later on fresh IPs. The catch: a single IP gets throttled after
  a few thousand fast requests, so for 1,000+ stores this is **iterative**, scrape, run
  `scrape retry` a few times over the next day, and the list shrinks each pass. For
  near-100% hands-off (no re-runs), use an **unblocker API** (Bright Data Web Unlocker
  or Oxylabs eBay Scraper API), which defeats the anti-bot per request, that is the
  paid upgrade if the manual re-run loop is too much.
- **Cost scales with proxy bandwidth on VPS workers.** Millions of items is doable
  but the proxy bill is real (section 7). The cheap-but-fiddly alternative is to run
  workers on residential-IP machines (no proxy needed), but eBay challenges a single
  residential IP after a few thousand fast requests, so that needs several machines
  at a modest rate. For a big one-off run, paying for proxy bandwidth is simplest.
- **eBay's per-store ceiling.** eBay stops paginating a store after ~10,000 items.
  Stores bigger than that are split by price range automatically, but it is an eBay
  limit, not a bug here.
- **The tool never fakes success.** A blocked crawl fails loudly with a message; it
  does not return a partial list pretending it finished.

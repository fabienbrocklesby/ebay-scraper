# eBay Scraper

Distributed eBay product scraper. It pulls full product data from eBay seller
store URLs at scale and writes it to CSV.

You run one **coordinator** machine (this can be a normal Windows PC) and as many
**worker** VPS nodes as you want. The coordinator holds the job queue and the
results database and runs the commands you type. The workers do the actual
scraping in parallel. Adding more workers increases speed with no code changes.

```
                 ┌─────────────────────────────┐
   you type ───▶ │  COORDINATOR (Windows is OK) │
   commands      │  - Redis    (job queue)      │
                 │  - Postgres (results)        │
                 │  - runs store crawl + export │
                 └──────────────┬──────────────┘
                                │ private Tailscale network
            ┌───────────────────┼───────────────────┐
            ▼                   ▼                   ▼
      ┌───────────┐       ┌───────────┐       ┌───────────┐
      │  worker    │      │  worker    │ ...   │  worker    │
      │ (a box     │      │ (a box     │       │ (a box     │
      │  with an   │      │  with an   │       │  with an   │
      │  IP)       │      │  IP)       │       │  IP)       │
      └───────────┘       └───────────┘       └───────────┘
   each worker fetches in parallel; add boxes to go faster
```

Each worker pulls item jobs from the queue and fetches the detail pages in
parallel (many at once per box, with a built-in per-IP rate limit). Adding more
worker boxes multiplies throughput with no code change.

**The one thing that decides your cost is what kind of IP your worker boxes have.**
This was measured against live eBay, and it matters a lot:

- **Worker on a residential IP** (a home PC, an office connection, a
  residential-IP box): eBay serves it directly. No proxy, **no per-GB bandwidth
  cost**, fast. This is the cheap path and the way to do millions/day without
  spending much. It is what Kieran's original home-PC script was already doing,
  just scaled up. Measured: ~6-8 items/sec per box. **But one IP has a reputation
  ceiling**: in a live soak a single residential IP ran clean for ~3,500
  back-to-back fetches at ~8/sec, then eBay started challenging it. So you do not
  get unlimited free throughput from one IP, you get it from **many** IPs. Reach
  millions/day by running several residential boxes (and/or lowering
  `MAX_RPS_PER_IP` so each IP stays under the challenge threshold). When an IP
  does get hot, the worker auto-falls back to the proxy for a cooldown, so data
  keeps flowing.
- **Worker on a datacenter/VPS IP** (Hetzner, DigitalOcean, Vultr, etc.): eBay
  blocks those IPs with a 403, so the worker must fetch through a **rotating
  residential proxy**, and every ~0.8 MB detail page then costs proxy bandwidth.
  Reliable, scales by adding boxes, but you pay per GB. Use this for burst
  capacity, not for a giant one-time backfill.

The worker handles both automatically: it tries its own IP first and only falls
back to the proxy when eBay blocks it. So the same build runs free on a
residential box and via-proxy on a datacenter box. Set the proxy once on the
coordinator (below) and it is there as the fallback whenever a box needs it.

Also required regardless of topology:

- **Tailscale** — a free private network so the workers can reach the
  coordinator's Redis/Postgres without exposing them to the internet.

---

## Quick start (operator)

1. `scraper setup` — paste your proxy URL (and optional Oxylabs credentials). It tests them and saves them.
2. `scraper doctor` — confirm everything is green (Redis, Postgres, Proxy, Workers).
3. Put your eBay store URLs in a text file, one per line (any marketplace, no other info needed).
4. `scraper run stores.txt` — scrapes everything and writes split CSV files to `exports/`.

The scraper auto-detects each store's home marketplace (US / AU / UK). You do not tag stores with a location or a niche. Make sure at least one worker is running (on a VPS, or locally) before `scraper run`, or it will wait for one.

---

## Table of contents

- [Quick mental model](#quick-mental-model)
- [Coordinator setup (Windows)](#coordinator-setup-windows)
- [Coordinator setup (macOS / Linux)](#coordinator-setup-macos--linux)
- [Proxy setup (required)](#proxy-setup-required)
- [Adding a worker VPS from scratch](#adding-a-worker-vps-from-scratch)
- [Daily use](#daily-use)
- [5-minute smoke test](#5-minute-smoke-test)
- [CSV output](#csv-output)
- [Command reference](#command-reference)
- [Troubleshooting](#troubleshooting)
- [Limits and honest expectations](#limits-and-honest-expectations)

---

## Quick mental model

- You install the `scraper` command on the coordinator and on every worker.
- On the coordinator you: start the services, set the proxy once, add store URLs,
  start a scrape, check progress, and export the CSV.
- On each worker you run **one** command (`scraper init <coordinator-ip>`) and it
  sets itself up and starts pulling jobs forever.
- The proxy is set **once** on the coordinator and every worker picks it up
  automatically. You never edit proxy settings on the workers.

---

## Coordinator setup (Windows)

The coordinator can be a normal Windows 10/11 PC. It needs three things.

### 1. Install Docker Desktop

Download Docker Desktop from <https://docs.docker.com/get-docker/> and install it.
On first run it will enable **WSL2** (Windows Subsystem for Linux). Accept that and
reboot if it asks. When Docker Desktop shows "Engine running" in the bottom-left,
it is ready.

To confirm, open **PowerShell** and run:

```powershell
docker version
```

You should see a Server section. If you only see a Client section, Docker Desktop
is not running yet — start it from the Start menu and wait.

### 2. Install Python and the scraper

Install Python 3.11 or newer from <https://www.python.org/downloads/> (tick
"Add Python to PATH" in the installer). Then in PowerShell:

```powershell
python -m pip install --user pipx
python -m pipx ensurepath
```

Close and reopen PowerShell, then install the scraper:

```powershell
pipx install git+https://github.com/OWNER/ebay-scraper
```

Confirm it works:

```powershell
scraper --help
```

### 3. Install Tailscale

Download Tailscale from <https://tailscale.com/download/windows>, install it, and
sign in. The Tailscale icon will appear in your system tray.

**Find your coordinator's Tailscale IP** (you will give this to each worker). The
easiest way on Windows is the Tailscale admin console: open
<https://login.tailscale.com/admin/machines>, find this PC, and copy its IP
(it looks like `100.x.y.z`). You can also right-click the tray icon; the IP is
shown there.

### 4. Start the coordinator services and database

In PowerShell:

```powershell
scraper coordinator start
scraper connect localhost
scraper db init
```

- `coordinator start` launches Redis and Postgres as Docker containers that
  restart automatically (including after a reboot).
- `connect localhost` writes the local config so the coordinator's own commands
  know where Redis and Postgres are.
- `db init` creates the database tables.

Check the services are up:

```powershell
scraper coordinator status
```

The coordinator is now ready. Next: [set the proxy](#proxy-setup-required).

> Note: the coordinator only runs the services and the orchestration commands
> (`store add`, `scrape start`, `export`). It does **not** run a worker. Workers
> run on Linux VPS nodes (see below). Do not run `scraper worker start` on the
> Windows coordinator.

---

## Coordinator setup (macOS / Linux)

Same as Windows, but Tailscale's command line is available, so you can get the IP
with a command.

```bash
# 1. Install Docker (Docker Desktop on macOS, or docker engine on Linux)
# 2. Install the scraper
pipx install git+https://github.com/OWNER/ebay-scraper
# 3. Install Tailscale and join your tailnet
#    macOS: https://tailscale.com/download     Linux: curl -fsSL https://tailscale.com/install.sh | sh
tailscale up
# 4. Start services
scraper coordinator start
scraper connect localhost
scraper db init
# 5. Get the IP + ready-made worker command to run on each VPS
scraper coordinator info
```

`scraper coordinator info` prints the coordinator's Tailscale IP and the exact
`scraper init ...` command to run on each worker.

---

## Proxy setup (the fallback)

Set a rotating residential proxy on the coordinator. It is the safety net the
whole system leans on: datacenter/VPS worker IPs are blocked by eBay and fetch
through it, residential worker boxes fall back to it only if their own IP gets
challenged, and the coordinator uses it if its IP is challenged while crawling a
big store. If all your worker boxes are residential and never get challenged, the
proxy may go almost unused, but set it anyway so nothing stalls when a block
happens.

A **rotating residential proxy** is one plan with a single endpoint that rotates
you through a large pool of real residential IPs. You are billed by bandwidth
(per GB), not per IP, so you buy one account, not "lots of proxies". Because
datacenter boxes route every page through it, proxy bandwidth is the main running
cost on a datacenter topology (about 1 GB per 1,000 items); residential-IP
workers avoid that cost.

### Recommended: IPRoyal (pay-as-you-go, no monthly commitment)

1. Sign up at <https://iproyal.com> and buy **Residential** traffic (not ISP, not
   Web Unblocker). The minimum is 1 GB (about 1,000 items) and the traffic never
   expires, so the first purchase doubles as your test credit.
2. In the IPRoyal dashboard, get your proxy username and password. The endpoint is
   `geo.iproyal.com:12321`.
3. Build the proxy URL with the username and password as-is, no country token:

   ```
   http://USERNAME:PASSWORD@geo.iproyal.com:12321
   ```

   You do **not** pick a country. The scraper detects each store's eBay site
   (`ebay.com`, `ebay.com.au`, `ebay.co.uk`, `ebay.de`, ...) and automatically
   routes that store through an exit IP in the matching country, so one credential
   scrapes any country's eBay correctly (right listings, right currency).

4. Set it once on the coordinator. Every worker picks it up automatically on its
   next job, with no restart:

   ```bash
   scraper proxy set "http://USERNAME:PASSWORD@geo.iproyal.com:12321"
   scraper proxy test
   ```

   `proxy test` fetches a live eBay item through the proxy and tells you whether
   it is working.

   Use the **plain** endpoint above (no session token). The scraper handles IP
   rotation itself: it gives each item its own connection (so every item scrapes
   from a fresh IP) and rotates the connection every few pages while crawling a
   store. That is what carries a crawl past eBay's per-IP block. This exact setup
   was validated end to end against a live store (5,400+ items pulled in one crawl,
   no blocks; 10/10 item pages scraped clean).

### Free option for first validation: Webshare

Webshare gives 10 rotating proxies and 1 GB/month free with no card
(<https://www.webshare.io>). It is fine to prove the pipeline works before paying
for IPRoyal. Set its proxy URL the same way with `scraper proxy set`.

### Cheapest option: rotating datacenter (e.g. ProxyScrape)

Datacenter proxies are far cheaper (often a few dollars, unmetered) but eBay
challenges them more often. The scraper handles that gracefully now: a challenged
item is retried automatically rather than silently dropped, so you lose speed, not
data. Usable on a tight budget; residential is more reliable.

To see or change the proxy later:

```bash
scraper proxy status      # show the active proxy (password masked)
scraper proxy set <url>   # change it for all workers
scraper proxy clear       # remove it (workers make direct requests)
```

---

## Adding a worker VPS from scratch

A worker is any fresh Linux VPS (Ubuntu 22.04 or Debian 12 recommended) from any
provider (Hetzner, DigitalOcean, Vultr, etc.). One command sets it up.

### 1. Create the VPS and log in

Create the VPS in your provider's panel and SSH in as root:

```bash
ssh root@<vps-public-ip>
```

### 2. Install the scraper

```bash
apt update && apt install -y python3-pip pipx
pipx ensurepath
# reopen the shell, or run: source ~/.bashrc
pipx install git+https://github.com/OWNER/ebay-scraper
```

### 3. Run the one-command setup

Get the coordinator's Tailscale IP (`scraper coordinator info` on the coordinator,
or the Tailscale admin console). Then on the VPS:

```bash
scraper init <coordinator-tailscale-ip>
```

This single command:

- installs Tailscale and joins your tailnet (it prints a URL — open it once in a
  browser to authorise the VPS; or pass `--tailscale-key tskey-...` to skip the
  prompt, keys come from <https://login.tailscale.com/admin/settings/keys>),
- installs Docker,
- writes the config pointing at your coordinator,
- builds the worker image and starts the worker container with
  `--restart=always` so it survives reboots.

That's it. The worker is now pulling jobs. You do **not** set the proxy here — it
comes from the coordinator automatically.

Repeat on as many VPS nodes as you want. They all share the one proxy and the one
queue.

### Confirm the worker is connected

On the coordinator:

```bash
scraper scrape status
```

Once you start a scrape, the "Queued jobs" number will fall as workers process it.

### Per-worker proxy (advanced, rarely needed)

The normal setup uses one shared proxy for all workers, set on the coordinator.
If you ever want a specific worker to use a different proxy (for example to split
load across two proxy accounts), pass it at init time:

```bash
scraper init <coordinator-ip> --proxy "http://USER:PASS@host:port"
```

A proxy set this way on a worker is only its local fallback; `scraper proxy set`
on the coordinator overrides it for all workers. For a single rotating account,
just use the coordinator setting and ignore this.

---

## Daily use

All of these run on the **coordinator**.

> **Simplest path:** if you just have a list of store URLs (any marketplace), use `scraper run stores.txt`. It imports, scrapes, and writes split CSVs to `exports/` in one command, no niche tagging required. The detailed flow below is for power users who want more control.

### Add stores to scrape

```bash
# one at a time (--niche is required here; use scraper run if you don't need niche tagging)
scraper store add https://www.ebay.com/str/sellername --niche car-parts

# or bulk, from a text file (one store per line: "URL" or "URL,niche")
scraper store import stores.txt --niche car-parts
scraper store list
```

`store import` file format: one store per line, either `URL` or `URL,niche`.
Blank lines and lines starting with `#` are ignored; a line's own niche wins,
`--niche` is the default for lines without one. So Kieran can keep a single
`stores.txt` and re-import it, or add stores one by one, whichever he prefers.

A `/str/` or `/sch/` seller URL both work; the tool converts to the canonical
store URL. The `--niche` tag groups items so you can export them separately later.

### Start scraping (backfill)

```bash
scraper scrape start                 # crawl all registered stores
scraper scrape start --niche car-parts   # only that niche
```

This crawls each store's pages on the coordinator, finds every item, and queues
the items as jobs in batches. The workers scrape them. If eBay blocks a store
mid-crawl, the command says so clearly and tells you which stores to re-run after
the proxy cools down (it does not silently return a partial list).

### Keep it fresh cheaply (delta)

After the first backfill of a store, you do not need to re-scrape everything to
stay current. `scrape delta` re-reads only the cheap listing pages (about 200
items per request), compares them to what is already in the database, and:

- queues a detail scrape for **new** items and items whose **price changed**,
- marks items that have **disappeared** from the store as inactive,
- skips everything unchanged (no detail-page fetch, no cost).

```bash
scraper scrape delta                   # delta-scan all stores
scraper scrape delta --niche car-parts # only that niche
```

The intended rhythm: run `scrape start` once per store to build the catalogue,
then run `scrape delta` on a schedule (for example a daily cron on the
coordinator) to keep it current for almost nothing. This is what keeps ongoing
cost tiny even when the initial catalogue is large.

### Tuning a worker box (throughput vs blocks)

Each worker box governs its own outbound rate. The defaults are conservative and
safe; raise them per box once you see how its IP behaves. Set these as
environment variables on the worker (they are also emitted by
`scraper worker docker-run`):

| Variable | Default | What it does |
|---|---|---|
| `WORKER_CONCURRENCY` | `8` | How many items the box fetches in parallel |
| `MAX_RPS_PER_IP` | `6` | Hard cap on items/sec for the box's IP (the main throttle) |
| `BATCH_SIZE` | `200` | Items per queued job |
| `CHALLENGE_ESCALATION_THRESHOLD` | `0.15` | Fraction of recent fetches blocked before the box switches itself to the proxy |
| `CHALLENGE_COOLDOWN_SECONDS` | `120` | How long it stays on the proxy after switching, before probing direct again |

On a residential box, raising `MAX_RPS_PER_IP` and `WORKER_CONCURRENCY` is how you
push a single box toward and past a million items/day; back off if you start
seeing blocks. On a datacenter box, the defaults route through the proxy
automatically.

### Check progress

```bash
scraper scrape status
```

Shows jobs still queued, total items scraped, a per-niche breakdown, and the
registered stores.

### Export to CSV

```bash
scraper export --output products.csv
scraper export --niche car-parts --output car-parts.csv
```

### Stop or clear

```bash
scraper scrape stop            # cancel pending jobs (in-flight jobs finish)
scraper clear --niche car-parts   # delete scraped rows for a niche
scraper store remove <url>     # unregister a store
```

---

## 5-minute smoke test

Run this once after setting everything up to prove the whole chain works. Use a
small store so it finishes quickly.

On the coordinator (services running, proxy set):

```bash
scraper store add https://www.ebay.com/str/hobbylinc --niche smoke-test
scraper scrape start --niche smoke-test
```

You should see something like `10 items found, 10 new jobs queued`.

On a worker (or wait for an existing worker), then back on the coordinator:

```bash
scraper scrape status     # Queued jobs should drop toward 0, Total scraped rise
scraper export --niche smoke-test --output smoke.csv
```

Open `smoke.csv`. You should have one row per item with title, price, condition,
images, item specifics and seller filled in. Then clean up:

```bash
scraper clear --niche smoke-test
scraper store remove https://www.ebay.com/str/hobbylinc
```

---

## CSV output

> **Heads up: the CSV is multi-currency.** Because stores span US/AU/UK, the `currency` column contains a mix of USD, AUD, and GBP. The prices are correct for each item's own marketplace. Do NOT map `price` straight into a single-currency Shopify store without first segmenting or converting by the `currency` column, or AUD/GBP prices will import as if they were USD.

Columns: `item_id, title, price, currency, condition, description, image_urls,
item_url, seller_id, store_url, category, item_specifics, mpn, upc, shipping,
listing_type, niche, scraped_at`

- `image_urls` — the full product image gallery, pipe-separated (`|`), at full
  resolution (`s-l1600`). Pulled from the item's image carousel, not just the
  handful in the page metadata. Images are not downloaded, only their URLs.
- `item_specifics` — JSON object of every eBay item specific (brand, model,
  colour, etc.).
- `mpn` — Manufacturer Part Number pulled from the item specifics.
- `upc` — UPC/EAN barcode pulled from the item specifics.
- `seller_id` — the store's seller.
- `niche` — the tag you set when adding the store.

Re-scraping an item updates its existing row (keyed on `item_id`), so running a
scrape again refreshes prices rather than creating duplicates.

---

## Command reference

| Command | Where | What it does |
|---|---|---|
| `scraper coordinator start` | coordinator | Start Redis + Postgres |
| `scraper coordinator stop` | coordinator | Stop them |
| `scraper coordinator status` | coordinator | Show their status |
| `scraper coordinator info` | coordinator | Print Tailscale IP + worker command |
| `scraper connect <host>` | any | Write config pointing at a coordinator |
| `scraper db init` | coordinator | Create database tables |
| `scraper proxy set <url>` | coordinator | Set the shared proxy for all workers |
| `scraper proxy test` | coordinator | Test the proxy against live eBay |
| `scraper proxy status` | coordinator | Show the active proxy |
| `scraper proxy clear` | coordinator | Remove the proxy |
| `scraper store add <url> --niche <tag>` | coordinator | Register one store |
| `scraper store import <file> [--niche <tag>]` | coordinator | Bulk-register stores from a text file |
| `scraper store list` | coordinator | List registered stores |
| `scraper store remove <url>` | coordinator | Unregister a store |
| `scraper scrape start [--niche <tag>]` | coordinator | Backfill: crawl stores, queue all item jobs |
| `scraper scrape delta [--niche <tag>]` | coordinator | Re-scan listings, queue only new/changed items, mark gone ones inactive |
| `scraper scrape status` | coordinator | Show progress |
| `scraper scrape stop` | coordinator | Cancel pending jobs |
| `scraper export [--niche <tag>] --output <file>` | coordinator | Write CSV |
| `scraper clear --niche <tag>` | coordinator | Delete scraped rows for a niche |
| `scraper init <coordinator-ip>` | worker VPS | One-command worker setup |
| `scraper worker start` | worker | Run a worker in the foreground (Linux) |

---

## Troubleshooting

**`scraper proxy test` says FAIL / a bot-challenge page.**
The proxy is being blocked. If it is a datacenter proxy, switch to a rotating
residential one (IPRoyal). If residential, check the username/password and that
you have bandwidth left in the account.

**`scrape start` reports a store was "BLOCKED".**
eBay challenged the coordinator's IP during the store crawl. Set a rotating
residential proxy (`scraper proxy set`) if you have not, and re-run
`scraper scrape start`. Already-queued items are not lost; only the blocked store
needs re-running.

**Workers are not picking up jobs (`scrape status` queue not dropping).**
Check the worker can reach the coordinator over Tailscale: on the worker,
`tailscale status` should list the coordinator, and `docker logs ebay-scraper-worker`
shows what it is doing. Make sure `scraper coordinator status` shows Redis and
Postgres up.

**Worker container keeps restarting.**
`docker logs ebay-scraper-worker` on the VPS shows the error. The most common
cause is a wrong coordinator IP or the coordinator services not running.

**macOS only: worker crashes with an `objc[...] fork()` error.**
This only affects running a worker directly on macOS (not production Linux
workers). Start it with `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES scraper worker
start`. Production workers run on Linux and are unaffected.

**Update the scraper to the latest version.**

```bash
pipx reinstall ebay-scraper
```

---

## Limits and honest expectations

- **Large stores have an eBay-imposed ceiling.** eBay's store pagination stops
  exposing items after roughly 10,000 per store. For stores bigger than that you
  cannot get every single item through pagination — that is an eBay limit, not a
  bug here. Most seller stores are well under it.
- **Throughput scales with the number of worker boxes, not with hammering one
  IP.** Each box runs at its `MAX_RPS_PER_IP` cap (default 6 items/sec). A live
  soak showed the real per-IP limit: a single residential IP fetched ~3,500 items
  back-to-back at ~8/sec with zero blocks, then eBay's per-IP reputation tripped
  and it began challenging (≈75% of requests in the next batch). Takeaways:
    - For a sustained backfill, keep `MAX_RPS_PER_IP` modest and **scale by adding
      residential boxes**, that is how you reach millions/day cheaply.
    - A single box pushed hard will get its IP challenged after a few thousand
      fast requests. That is eBay's reputation system, not a bug. When it happens
      the worker auto-falls back to the proxy for `CHALLENGE_COOLDOWN_SECONDS`
      then probes direct again, so the backfill continues (paying proxy bandwidth
      only while the IP is hot).
    - Watch the first long backfill and tune `MAX_RPS_PER_IP` down if you see a
      box leaning on the proxy a lot.
- **Cost depends on worker IP type, not on the tool.** On residential-IP boxes
  the detail fetches are direct and free, so a large one-time backfill costs
  essentially nothing but time. On datacenter/VPS boxes the same fetches go
  through the residential proxy and cost bandwidth: each item page is roughly
  0.8-1 MB, so budget about 1 GB per 1,000 items of proxy traffic. The cheap way
  to do a big backfill is residential-IP workers; reserve datacenter+proxy boxes
  for burst capacity. After backfill, `scrape delta` keeps things current for
  almost nothing on either topology.
- **The store crawl (discovery) runs on the coordinator.** If the coordinator's
  own IP gets challenged while crawling a large store's listing pages, set the
  proxy (`scraper proxy set`) so discovery routes through it too; the crawl fails
  loudly rather than returning a partial list.

---

## Running the tests (development)

```bash
docker compose up -d
docker exec $(docker ps -qf "ancestor=postgres:16-alpine") psql -U scraper -d ebayscraper -c "CREATE DATABASE ebayscraper_test;" 2>/dev/null || true
pip install -e ".[test]"
pytest -v
```

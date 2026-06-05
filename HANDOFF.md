# eBay Scraper, Kieran's Setup Guide

This takes you from nothing to a folder full of product spreadsheets (CSV files) ready
for Shopify. Follow it top to bottom. You do not need to understand the tech; just copy
the commands into each server exactly as written and fill in your own details where it
says so.

Repo: **https://github.com/fabienbrocklesby/ebay-scraper** (public, so every `git clone`
below just works, no login or token needed).

---

## 1. How it works (the simple version)

Think of it as a tiny factory you rent by the hour:

- **The Coordinator** is the manager. It keeps the list of shops to visit, the to-do
  list, and the filing cabinet of results. You type your commands here. It is one small
  server.
- **The Workers** are the staff. Each one grabs jobs off the to-do list and downloads
  product pages. Hire more staff and the job finishes faster. They all share one to-do
  list, so no two ever do the same job. Each worker is its own small server.
- **IPRoyal** is a wardrobe of disguises. eBay blocks robots, so every page is fetched
  wearing a "normal person at home" disguise, from the right country.
- **Oxylabs** is a master key. A handful of pages the disguises can't open, the master
  key opens. The tool uses it sparingly (mainly just to work out which country a shop
  belongs to), so it stays cheap.
- **Tailscale** is a private hallway that connects the manager and the staff so no
  stranger can wander in.

You drop a text file of shop links, type one command, and a while later you get
spreadsheet files full of every product. **The tool works out which country each shop is
in (US, UK, Australia) on its own**, you never tag anything.

```
   COORDINATOR (the manager)                 WORKERS (the staff, add as many as you like)
   - keeps the to-do list      <- private ->   worker 1  ─┐
   - keeps the results              hallway     worker 2  ─┼─ all share one to-do list
   - you type commands here       (Tailscale)   worker N  ─┘
        |                                            |
        |  you: scraper run stores.txt               |  each: downloads product pages
        v                                            v   wearing an IPRoyal disguise
   CSV spreadsheets  <------------------------  saved to the filing cabinet
```

---

## 2. What to sign up for (about 15 minutes)

1. **Servers (VPSs).** Rent cheap Linux servers. You need **one Coordinator** plus **one
   or more Workers** (more workers = faster). Recommended: **Hetzner Cloud**
   (<https://www.hetzner.com/cloud>), a "CPX11" (about EUR 4-5/month, or pennies per hour)
   running **Ubuntu 22.04**. Vultr or DigitalOcean work too. For a big one-off run, rent
   several workers by the hour and delete them when done.

2. **IPRoyal residential proxy** (<https://iproyal.com> > Residential). After you buy
   some traffic, your proxy link looks like:
   `http://USERNAME:PASSWORD@geo.iproyal.com:12321`. Keep credit on it; if it runs dry,
   scraping just stops. Budget roughly **1 GB per ~7,000 products**.

3. **Oxylabs** (<https://oxylabs.io> > Web Scraper API / E-Commerce Scraper API). You get
   a username and password. This is the "master key" that makes sure shops from every
   country get found. It is used lightly, so it costs very little (see cost section).

4. **Tailscale** (<https://tailscale.com>, free). One account. You will install it on the
   Coordinator and on every Worker, all signed into this same account.

You will paste two things during setup, so keep them handy:
- Your **IPRoyal proxy link**: `http://USERNAME:PASSWORD@geo.iproyal.com:12321`
- Your **Oxylabs username and password**.

---

## 3. Set up the Coordinator (do this once)

Rent one Ubuntu 22.04 server, then connect to it (`ssh root@SERVER_IP`) and paste these
blocks one at a time.

```bash
# Install the basics
apt update && apt install -y python3-pip git docker.io curl

# Join the private network (Tailscale). Open the link it prints to sign in.
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up
tailscale ip -4          # write down the 100.x.y.z address it prints, you need it for workers

# Get the program
git clone https://github.com/fabienbrocklesby/ebay-scraper.git
cd ebay-scraper
pip install -e . --break-system-packages

# Start the manager's services and database
scraper coordinator start
scraper connect localhost
scraper db init
```

Now paste your credentials. This command asks for your IPRoyal proxy link, tests it, then
asks if you want to add the Oxylabs master key (say yes, paste username and password):

```bash
scraper setup
```

It will tell you which countries your proxy is clean for, and that any flagged ones are
covered by Oxylabs. That is normal and fine.

Check everything is healthy:

```bash
scraper doctor
```

You want green `[OK ]` on Redis, Postgres, Proxy, and Unblocker. Workers will show 0 for
now (you add them next).

Finally, get the exact command to run on your workers and copy it somewhere:

```bash
scraper coordinator info
```

Leave the Coordinator running. It is the manager; the workers need it on.

---

## 4. Set up each Worker (repeat for every worker server)

Rent another Ubuntu 22.04 server, connect to it, and paste:

```bash
# Basics + private network (same Tailscale account as the Coordinator)
apt update && apt install -y python3-pip git docker.io curl
curl -fsSL https://tailscale.com/install.sh | sh
tailscale up

# Get the program
git clone https://github.com/fabienbrocklesby/ebay-scraper.git
cd ebay-scraper
pip install -e . --break-system-packages
```

Then run **the command that `scraper coordinator info` printed** in the last step (it
looks like `scraper init 100.x.y.z ...`). That one command sets the worker up, points it
at the Coordinator, and starts it. The worker now pulls jobs automatically and picks up
the proxy and Oxylabs settings from the Coordinator on its own.

That's the whole worker. Want it faster? Spin up another server and do this section again.
There is no limit; they all share one to-do list.

Back on the Coordinator, confirm a worker has joined:

```bash
scraper doctor       # Workers should now show 1 (or more) connected
```

---

## 5. Run it (this is the daily part, all on the Coordinator)

1. Make a plain text file of shop links, **one per line**. Any country, no tags. Both
   `https://www.ebay.com/str/SHOPNAME` and the seller-search form
   `https://www.ebay.com/sch/i.html?_ssn=SELLERNAME` work.

   ```bash
   nano stores.txt
   # paste your links, one per line, then Ctrl+O Enter Ctrl+X to save
   ```

2. Run it:

   ```bash
   scraper run stores.txt
   ```

That's it. It imports the shops, works out each one's country, scrapes every product, and
writes spreadsheet files into an `exports/` folder. While it runs it prints each shop as
`OK` (found and scraping), `unresolved` (couldn't read it this time, saved to retry), or
`blocked`. At the end it prints a summary (shops done, products, files written).

Get your files:

```bash
ls exports/          # products_001.csv, products_002.csv, ...
```

Each row has: title, price, currency, condition, description, the **full image gallery**
(full-resolution links), item link, brand/model and other specifics, and more, exactly
what Shopify needs.

> **The spreadsheets are multi-currency.** Because shops span US/UK/Australia, the
> `currency` column is a mix of USD, AUD, GBP. Each price is correct for its own country.
> Before importing into one Shopify store, split or convert the prices by the `currency`
> column, otherwise a $20 AUD item would import as $20 USD.

### Keeping it fresh later (optional, cheap)

After the first full run, you don't need to re-scrape everything to stay current:

```bash
scraper scrape delta        # only re-checks for new items and price changes
```

### If some shops came back "unresolved"

That just means the tool couldn't confidently read them on that pass (usually a busy
proxy). Run this later and it retries only those:

```bash
scraper scrape retry
```

---

## 6. Cost (real numbers)

- **Servers:** ~$5/month each, or pennies/hour. One worker scrapes ~190,000 products/day.
  Rent more for a big run, delete them after.
- **IPRoyal:** about **1 GB per ~7,000 products**, roughly **$2/GB** in bulk, so about
  **$30 per 100,000 products**. Buy a bulk traffic package for large runs.
- **Oxylabs:** used mainly to identify each shop's country, about **3 requests per shop,
  one time**, roughly **$6 across 1,500 shops**. Tiny. (It only does heavy lifting if
  your proxy is badly flagged for a country.)

---

## 7. If something looks wrong

- **`scraper doctor` shows Proxy flagged for a country.** Normal. Oxylabs covers it. As
  long as Proxy shows `[OK ]` (clean for at least one country) and Unblocker shows
  `[OK ]`, you are fine.
- **Lots of shops come back "unresolved."** Your proxy account may be worn out for a
  country (often from heavy testing). Easiest fix: get a **fresh IPRoyal account/pool**.
  Make sure Oxylabs is set up (`scraper setup`), which also covers it.
- **Jobs do nothing / a "402" appears.** IPRoyal is out of credit. Top it up.
- **A worker isn't joining.** On the worker run `tailscale status` (should list the
  Coordinator) and `docker logs ebay-scraper-worker`. On the Coordinator,
  `scraper doctor` shows the live picture.
- **Updating to a newer version** (on the Coordinator and each worker):
  ```bash
  cd ebay-scraper && git pull && pip install -e . --break-system-packages
  scraper db init           # Coordinator only; safely adds any new database fields
  ```
  On each worker, also re-run the `scraper init ...` command after pulling.

---

## 8. Honest limits (no surprises)

- **It tells the truth.** If it can't confidently read a shop, it marks it `unresolved`
  and saves it for retry rather than guessing and giving you wrong-country prices.
- **Some shops are on non-US eBay** (e.g. Australian sellers on ebay.com.au). The tool
  finds and routes these automatically; that is the whole point of the marketplace
  detection. It needs either a proxy that can read that country, or Oxylabs configured, so
  set up Oxylabs and you're covered.
- **eBay caps a single shop at ~10,000 items.** Bigger shops are split by price
  automatically. That's an eBay limit, not a bug.
- **Cost scales with how many products you pull** (proxy traffic). Millions is doable; buy
  a bulk traffic package.

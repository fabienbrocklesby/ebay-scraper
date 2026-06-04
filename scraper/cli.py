import asyncio
import json
import shutil
import subprocess
import sys
from importlib.resources import as_file, files
from pathlib import Path

import asyncpg
import click

from scraper.config import Settings
from scraper.db import (
    add_store, clear_niche, get_counts, init_schema, list_stores, remove_store,
)
from scraper.export import export_to_csv
from scraper.fetch import ChallengeError
from scraper.queue import enqueue_items, get_queue, get_redis, PROXY_REDIS_KEY
from scraper.scraper import scrape_item
from scraper.store import _sch_to_str_url, _normalize_store_url, get_item_urls_from_store
from scraper.worker import start_worker

_CONFIG_DIR = Path.home() / ".config" / "ebay-scraper"
_COMPOSE_PROJECT = "ebay-scraper"
_WORKER_CONTAINER = "ebay-scraper-worker"
_WORKER_IMAGE = "ebay-scraper-worker"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_command_available(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _tailscale_bin() -> str | None:
    """Locate the tailscale CLI.

    On Windows the CLI installs to Program Files and is not on PATH by default,
    so the bare 'tailscale' command fails there. Fall back to the known install
    locations before giving up.
    """
    found = shutil.which("tailscale")
    if found:
        return found
    candidates = [
        Path(r"C:\Program Files\Tailscale\tailscale.exe"),
        Path(r"C:\Program Files (x86)\Tailscale\tailscale.exe"),
        Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


def _get_tailscale_ip() -> str | None:
    binary = _tailscale_bin()
    if not binary:
        return None
    result = subprocess.run([binary, "ip", "-4"], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _tailscale_connected() -> bool:
    binary = _tailscale_bin()
    if not binary:
        return False
    result = subprocess.run([binary, "status", "--json"], capture_output=True, text=True)
    if result.returncode != 0:
        return False
    try:
        state = json.loads(result.stdout).get("BackendState", "")
        return state == "Running"
    except (json.JSONDecodeError, AttributeError):
        return False


def _find_source_dir() -> Path | None:
    # Editable install: __file__ is inside the actual source tree
    project_root = Path(__file__).parent.parent
    if (project_root / "Dockerfile").exists() and (project_root / "scraper").is_dir():
        return project_root
    # Common deployment locations
    for candidate in [Path("/opt/ebay-scraper"), Path.home() / "ebay-scraper"]:
        if candidate.exists() and (candidate / "Dockerfile").exists():
            return candidate
    return None


def _write_config(host: str, db_password: str, db_user: str, db_name: str,
                  redis_port: int, db_port: int, proxy: str, rate: float) -> Path:
    redis_url = f"redis://{host}:{redis_port}"
    db_url = f"postgresql://{db_user}:{db_password}@{host}:{db_port}/{db_name}"
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    env_path = _CONFIG_DIR / ".env"
    env_path.write_text(
        f"REDIS_URL={redis_url}\n"
        f"DATABASE_URL={db_url}\n"
        f"PROXY_URL={proxy}\n"
        f"REQUESTS_PER_SECOND={rate}\n"
    )
    return env_path


def _docker_run_worker(settings: Settings) -> None:
    proxy = settings.proxy_url or ""
    # Stop and remove existing container if present
    subprocess.run(["docker", "stop", _WORKER_CONTAINER], capture_output=True)
    subprocess.run(["docker", "rm", _WORKER_CONTAINER], capture_output=True)
    subprocess.run([
        "docker", "run", "-d",
        "--restart=always",
        "--name", _WORKER_CONTAINER,
        "-e", f"REDIS_URL={settings.redis_url}",
        "-e", f"DATABASE_URL={settings.database_url}",
        "-e", f"PROXY_URL={proxy}",
        "-e", f"REQUESTS_PER_SECOND={settings.requests_per_second}",
        _WORKER_IMAGE,
    ], check=True)


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@click.group()
def cli() -> None:
    """eBay scraper: queue stores, run workers, export results."""


@cli.command()
@click.argument("coordinator_host")
@click.option("--tailscale-key", envvar="TAILSCALE_AUTH_KEY", default="",
              help="Tailscale auth key from tailscale.com/admin/settings/keys (or set TAILSCALE_AUTH_KEY env var)")
@click.option("--proxy", default="", help="Rotating residential proxy URL (http://user:pass@host:port)")
@click.option("--db-password", default="scraper", show_default=True)
@click.option("--db-user", default="scraper", show_default=True)
@click.option("--db-name", default="ebayscraper", show_default=True)
@click.option("--rate", default=0.5, show_default=True, help="Requests per second")
def init(coordinator_host: str, tailscale_key: str, proxy: str,
         db_password: str, db_user: str, db_name: str, rate: float) -> None:
    """Set up this machine as a scraper worker (run once per VPS).

    COORDINATOR_HOST is the coordinator's Tailscale IP or hostname.
    Get it by running 'scraper coordinator info' on the coordinator machine.
    """
    click.echo("=== eBay Scraper Worker Setup ===\n")

    # 1. Tailscale
    click.echo("[1/5] Tailscale")
    if not _is_command_available("tailscale"):
        if sys.platform.startswith("linux"):
            click.echo("  Installing Tailscale...")
            subprocess.run(
                ["sh", "-c", "curl -fsSL https://tailscale.com/install.sh | sh"],
                check=True,
            )
        else:
            click.echo("  Tailscale not found.")
            click.echo("  Install from: https://tailscale.com/download")
            click.echo("  Then run this command again.")
            sys.exit(1)

    if _tailscale_connected():
        ts_ip = _get_tailscale_ip()
        click.echo(f"  Connected ({ts_ip})")
    else:
        click.echo("  Connecting to Tailscale...")
        if tailscale_key:
            subprocess.run(["tailscale", "up", "--authkey", tailscale_key], check=True)
        else:
            click.echo("")
            click.echo("  No auth key provided. Starting interactive setup.")
            click.echo("  Tailscale will print a URL. Visit it in your browser to authorize this device.\n")
            result = subprocess.run(["tailscale", "up"])
            if result.returncode != 0:
                click.echo("\n  Tailscale connection failed.", err=True)
                sys.exit(1)

        if not _tailscale_connected():
            click.echo("  Not connected after setup. Run 'tailscale status' to diagnose.", err=True)
            sys.exit(1)
        ts_ip = _get_tailscale_ip()
        click.echo(f"  Connected ({ts_ip})")

    # 2. Docker
    click.echo("[2/5] Docker")
    if not _is_command_available("docker"):
        if sys.platform.startswith("linux"):
            click.echo("  Installing Docker...")
            subprocess.run(
                ["sh", "-c", "curl -fsSL https://get.docker.com | sh"],
                check=True,
            )
            subprocess.run(["systemctl", "start", "docker"], check=True)
            subprocess.run(["systemctl", "enable", "docker"], check=True)
        else:
            click.echo("  Docker not found.")
            click.echo("  Install Docker Desktop from: https://docs.docker.com/get-docker/")
            sys.exit(1)
    else:
        click.echo("  Installed")

    # 3. Config
    click.echo("[3/5] Config")
    env_path = _write_config(
        host=coordinator_host,
        db_password=db_password,
        db_user=db_user,
        db_name=db_name,
        redis_port=6379,
        db_port=5432,
        proxy=proxy,
        rate=rate,
    )
    click.echo(f"  Written to {env_path}")
    click.echo(f"  Redis:    redis://{coordinator_host}:6379")
    click.echo(f"  Postgres: postgresql://{db_user}:***@{coordinator_host}:5432/{db_name}")

    # 4. Build Docker image
    click.echo("[4/5] Worker image")
    source_dir = _find_source_dir()
    if source_dir is None:
        click.echo("  Source directory not found.", err=True)
        click.echo("  Clone the repo and run from there, or use --source-dir.", err=True)
        sys.exit(1)
    click.echo(f"  Building from {source_dir} ...")
    subprocess.run(["docker", "build", "-t", _WORKER_IMAGE, str(source_dir)], check=True)
    click.echo("  Built")

    # 5. Start worker
    click.echo("[5/5] Starting worker")
    redis_url = f"redis://{coordinator_host}:6379"
    db_url = f"postgresql://{db_user}:{db_password}@{coordinator_host}:5432/{db_name}"
    subprocess.run(["docker", "stop", _WORKER_CONTAINER], capture_output=True)
    subprocess.run(["docker", "rm", _WORKER_CONTAINER], capture_output=True)
    subprocess.run([
        "docker", "run", "-d",
        "--restart=always",
        "--name", _WORKER_CONTAINER,
        "-e", f"REDIS_URL={redis_url}",
        "-e", f"DATABASE_URL={db_url}",
        "-e", f"PROXY_URL={proxy}",
        "-e", f"REQUESTS_PER_SECOND={rate}",
        _WORKER_IMAGE,
    ], check=True)
    click.echo(f"  Container '{_WORKER_CONTAINER}' started (restart=always)")

    click.echo("\nDone. Worker is running and will restart on reboot.")
    click.echo("Run 'scraper scrape status' on the coordinator to confirm it is pulling jobs.")


# ---------------------------------------------------------------------------
# coordinator
# ---------------------------------------------------------------------------


@cli.group()
def coordinator() -> None:
    """Coordinator services (Redis + Postgres). Run on the coordinator machine."""


@coordinator.command("start")
def coordinator_start() -> None:
    """Start Redis and Postgres on this machine."""
    compose_ref = files("scraper") / "coordinator-compose.yml"
    with as_file(compose_ref) as compose_path:
        subprocess.run(
            ["docker", "compose", "-p", _COMPOSE_PROJECT, "-f", str(compose_path), "up", "-d"],
            check=True,
        )
    click.echo("Coordinator started.")
    click.echo("  Redis:    localhost:6379")
    click.echo("  Postgres: localhost:5432  (user: scraper, db: ebayscraper)")


@coordinator.command("stop")
def coordinator_stop() -> None:
    """Stop coordinator services."""
    compose_ref = files("scraper") / "coordinator-compose.yml"
    with as_file(compose_ref) as compose_path:
        subprocess.run(
            ["docker", "compose", "-p", _COMPOSE_PROJECT, "-f", str(compose_path), "down"],
            check=True,
        )
    click.echo("Coordinator stopped.")


@coordinator.command("status")
def coordinator_status() -> None:
    """Show coordinator container status."""
    compose_ref = files("scraper") / "coordinator-compose.yml"
    with as_file(compose_ref) as compose_path:
        subprocess.run(
            ["docker", "compose", "-p", _COMPOSE_PROJECT, "-f", str(compose_path), "ps"],
            check=True,
        )


@coordinator.command("info")
def coordinator_info() -> None:
    """Print the init command to run on each worker VPS."""
    ts_ip = _get_tailscale_ip()
    if not ts_ip:
        click.echo("Tailscale not connected on this machine.")
        click.echo("Install Tailscale and run 'tailscale up', then run this again.")
        return

    click.echo(f"Coordinator Tailscale IP: {ts_ip}\n")
    click.echo("Run this on each worker VPS:")
    click.echo(f"\n  scraper init {ts_ip}\n")
    click.echo("With a Tailscale auth key (non-interactive):")
    click.echo(f"  scraper init {ts_ip} --tailscale-key tskey-auth-XXXXXX\n")
    click.echo("With a residential proxy:")
    click.echo(f"  scraper init {ts_ip} --proxy http://user:pass@host:port\n")
    click.echo("Get auth keys at: https://login.tailscale.com/admin/settings/keys")


# ---------------------------------------------------------------------------
# connect (manual alternative to init)
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("host")
@click.option("--db-password", default="scraper", show_default=True)
@click.option("--db-user", default="scraper", show_default=True)
@click.option("--db-name", default="ebayscraper", show_default=True)
@click.option("--redis-port", default=6379, show_default=True)
@click.option("--db-port", default=5432, show_default=True)
@click.option("--proxy", default="", help="Rotating residential proxy URL")
@click.option("--rate", default=0.5, show_default=True, help="Requests per second")
def connect(host: str, db_password: str, db_user: str, db_name: str,
            redis_port: int, db_port: int, proxy: str, rate: float) -> None:
    """Configure this machine to connect to a coordinator (manual alternative to init).

    HOST is the coordinator's IP address or Tailscale hostname.
    """
    env_path = _write_config(
        host=host,
        db_password=db_password,
        db_user=db_user,
        db_name=db_name,
        redis_port=redis_port,
        db_port=db_port,
        proxy=proxy,
        rate=rate,
    )
    click.echo(f"Config written to {env_path}")
    click.echo(f"  Redis:    redis://{host}:{redis_port}")
    click.echo(f"  Postgres: postgresql://{db_user}:***@{host}:{db_port}/{db_name}")
    click.echo("")
    click.echo("Start a worker:  scraper worker start")
    click.echo("Docker command:  scraper worker docker-run")


# ---------------------------------------------------------------------------
# worker
# ---------------------------------------------------------------------------


@cli.group()
def worker() -> None:
    """Worker commands."""


@worker.command("start")
def worker_start() -> None:
    """Start a scrape worker on this machine (runs until Ctrl+C)."""
    settings = Settings()
    click.echo("Starting worker. Press Ctrl+C to stop.")
    start_worker(settings.redis_url)


@worker.command("docker-run")
@click.option("--image", default=_WORKER_IMAGE, show_default=True)
def worker_docker_run(image: str) -> None:
    """Print the docker run command for this machine's config."""
    settings = Settings()
    proxy = settings.proxy_url or ""
    click.echo(
        f"docker run -d --restart=always --name {_WORKER_CONTAINER} \\\n"
        f"  -e REDIS_URL={settings.redis_url} \\\n"
        f"  -e DATABASE_URL={settings.database_url} \\\n"
        f"  -e PROXY_URL={proxy} \\\n"
        f"  -e REQUESTS_PER_SECOND={settings.requests_per_second} \\\n"
        f"  {image}"
    )


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


@cli.group()
def store() -> None:
    """Manage the list of stores to scrape."""


def _canonical_store_url(url: str) -> str:
    converted = _sch_to_str_url(url)
    return _normalize_store_url(converted if converted else url)


@store.command("add")
@click.argument("url")
@click.option("--niche", required=True, help="Tag for this store (e.g. electronics)")
def store_add(url: str, niche: str) -> None:
    """Register a store URL for scraping.

    Run 'scraper scrape start' to begin scraping all registered stores.
    URL can be an eBay /str/ or /sch/ seller URL.
    """
    settings = Settings()
    canonical = _canonical_store_url(url)
    if canonical != url:
        click.echo(f"  (converted to canonical URL: {canonical})")

    async def _run() -> None:
        pool = await asyncpg.create_pool(settings.database_url)
        try:
            await add_store(pool, canonical, niche)
        finally:
            await pool.close()

    asyncio.run(_run())
    click.echo(f"Store registered: {canonical}  [{niche}]")
    click.echo("Run 'scraper scrape start' to begin scraping.")


@store.command("list")
def store_list() -> None:
    """List all registered stores."""
    settings = Settings()

    async def _run() -> list:
        pool = await asyncpg.create_pool(settings.database_url)
        try:
            return await list_stores(pool)
        finally:
            await pool.close()

    stores = asyncio.run(_run())
    if not stores:
        click.echo("No stores registered. Use 'scraper store add <url> --niche <tag>'")
        return
    for s in stores:
        click.echo(f"  {s['store_url']}  [{s['niche']}]  added {s['added_at'].strftime('%Y-%m-%d')}")


@store.command("remove")
@click.argument("url")
def store_remove(url: str) -> None:
    """Remove a store from the registered list."""
    settings = Settings()
    canonical = _canonical_store_url(url)

    async def _run() -> bool:
        pool = await asyncpg.create_pool(settings.database_url)
        try:
            return await remove_store(pool, canonical)
        finally:
            await pool.close()

    found = asyncio.run(_run())
    if found:
        click.echo(f"Removed: {canonical}")
    else:
        click.echo(f"Not found: {canonical}", err=True)


# ---------------------------------------------------------------------------
# scrape
# ---------------------------------------------------------------------------


@cli.group()
def scrape() -> None:
    """Control scraping (start, stop, status)."""


@scrape.command("start")
@click.option("--niche", default=None, help="Only scrape stores with this niche tag")
def scrape_start(niche: str | None) -> None:
    """Paginate all registered stores and queue item scrape jobs for VPS workers.

    Store pagination runs here on the coordinator (residential IP, no proxy needed).
    VPS workers handle the actual item scraping in parallel.
    """
    settings = Settings()

    async def _get_stores() -> list:
        pool = await asyncpg.create_pool(settings.database_url)
        try:
            return await list_stores(pool)
        finally:
            await pool.close()

    stores = asyncio.run(_get_stores())
    if niche:
        stores = [s for s in stores if s["niche"] == niche]

    if not stores:
        click.echo("No stores registered. Use 'scraper store add <url> --niche <tag>'")
        return

    redis_conn = get_redis(settings.redis_url)
    queue = get_queue(redis_conn)

    raw_proxy = redis_conn.get(PROXY_REDIS_KEY)
    proxy_url = raw_proxy.decode().strip() if raw_proxy else settings.proxy_url
    proxy_url = proxy_url or None

    total_queued = 0
    blocked: list[str] = []

    for s in stores:
        click.echo(f"  Crawling {s['store_url']} ...")
        try:
            item_urls = get_item_urls_from_store(
                s["store_url"],
                proxy_url=proxy_url,
                requests_per_second=settings.requests_per_second,
            )
        except ChallengeError as exc:
            blocked.append(s["store_url"])
            click.echo(f"    BLOCKED: {exc}", err=True)
            continue
        queued = enqueue_items(queue, redis_conn, item_urls, niche=s["niche"], store_url=s["store_url"])
        click.echo(f"    {len(item_urls)} items found, {queued} new jobs queued  [{s['niche']}]")
        total_queued += queued

    click.echo(f"\nDone. {total_queued} item jobs queued for workers.")
    if blocked:
        click.echo(
            f"\n{len(blocked)} store(s) were blocked by eBay before the crawl "
            f"completed and were NOT fully queued:", err=True,
        )
        for url in blocked:
            click.echo(f"  - {url}", err=True)
        click.echo(
            "Set a rotating residential proxy (scraper proxy set <url>) and "
            "re-run 'scraper scrape start' to finish them.", err=True,
        )


@scrape.command("delta")
@click.option("--niche", default=None, help="Only delta-scan stores with this niche tag.")
def scrape_delta(niche: str | None) -> None:
    """Re-scan store listing pages and queue only new or price-changed items."""
    from scraper.store import get_store_listings
    from scraper.delta import compute_delta
    from scraper.db import get_store_item_prices, mark_items_inactive
    from scraper.queue import mark_item_queued
    from scraper.worker import scrape_batch

    settings = Settings()
    redis_conn = get_redis(settings.redis_url)
    queue = get_queue(redis_conn)
    raw_proxy = redis_conn.get(PROXY_REDIS_KEY)
    proxy_url = raw_proxy.decode().strip() if raw_proxy else settings.proxy_url
    proxy_url = proxy_url or None

    async def _run() -> None:
        pool = await asyncpg.create_pool(settings.database_url)
        try:
            stores = await list_stores(pool)
            selected = [s for s in stores if not niche or s["niche"] == niche]
            if not selected:
                click.echo("No stores registered. Use 'scraper store add <url> --niche <tag>'")
                return
            for s in selected:
                store_url = s["store_url"]
                try:
                    listings = get_store_listings(store_url, proxy_url=proxy_url)
                except ChallengeError as exc:
                    click.echo(f"  {store_url} BLOCKED: {exc}", err=True)
                    continue
                db_prices = await get_store_item_prices(pool, store_url)
                to_fetch, to_deactivate = compute_delta(
                    [(item_id, price) for (item_id, price, _url) in listings], db_prices
                )
                url_by_id = {item_id: url for (item_id, _price, url) in listings}
                fetch_urls = [url_by_id[item_id] for item_id in to_fetch]
                for item_id in to_fetch:
                    mark_item_queued(redis_conn, item_id)
                queued = 0
                for j in range(0, len(fetch_urls), settings.batch_size):
                    batch = fetch_urls[j : j + settings.batch_size]
                    queue.enqueue(scrape_batch, batch, s["niche"], store_url, 0, job_timeout=600)
                    queued += len(batch)
                deactivated = await mark_items_inactive(pool, store_url, to_deactivate)
                click.echo(f"{store_url}: {queued} queued, {deactivated} deactivated")
        finally:
            await pool.close()

    asyncio.run(_run())


@scrape.command("stop")
def scrape_stop() -> None:
    """Cancel all pending scrape jobs in the queue.

    In-flight jobs on workers finish normally. Only pending jobs are removed.
    """
    settings = Settings()
    redis_conn = get_redis(settings.redis_url)
    queue = get_queue(redis_conn)
    count = len(queue)
    queue.empty()
    click.echo(f"Cleared {count} pending jobs from queue.")


@scrape.command("status")
def scrape_status() -> None:
    """Show scrape progress: queue depth, scraped counts, and registered stores."""
    settings = Settings()
    redis_conn = get_redis(settings.redis_url)
    queue = get_queue(redis_conn)

    async def _run() -> tuple:
        pool = await asyncpg.create_pool(settings.database_url)
        try:
            counts = await get_counts(pool)
            stores = await list_stores(pool)
        finally:
            await pool.close()
        return counts, stores

    counts, stores = asyncio.run(_run())
    total = sum(counts.values())
    click.echo(f"Queued jobs:   {len(queue)}")
    click.echo(f"Total scraped: {total}")
    if counts:
        click.echo("\nPer-niche:")
        for n, c in sorted(counts.items()):
            click.echo(f"  {n}: {c}")
    if stores:
        click.echo(f"\nRegistered stores ({len(stores)}):")
        for s in stores:
            click.echo(f"  {s['store_url']}  [{s['niche']}]")


# ---------------------------------------------------------------------------
# proxy
# ---------------------------------------------------------------------------


@cli.group()
def proxy() -> None:
    """Manage the rotating proxy used by all workers."""


@proxy.command("set")
@click.argument("proxy_url")
def proxy_set(proxy_url: str) -> None:
    """Set the proxy URL for all workers (takes effect on next job, no restart needed).

    PROXY_URL format: http://user:pass@host:port

    Workers read the proxy from Redis on every job, so this takes effect
    immediately across all VPS workers without restarting anything.

    Use a rotating residential proxy's plain endpoint (no sticky-session token).
    eBay blocks any single IP after ~20-40 requests, so a fixed IP cannot crawl a
    large store or feed multiple workers. The scraper rotates IPs itself: each item
    scrape uses its own session (one fresh IP per item) and the store crawl rebuilds
    its session every few pages, so each connection stays under eBay's per-IP limit.
    Verify with 'scraper proxy test'.
    """
    settings = Settings()
    redis_conn = get_redis(settings.redis_url)
    redis_conn.set(PROXY_REDIS_KEY, proxy_url)

    env_path = _CONFIG_DIR / ".env"
    if env_path.exists():
        lines = env_path.read_text().splitlines()
        updated = False
        new_lines = []
        for line in lines:
            if line.startswith("PROXY_URL="):
                new_lines.append(f"PROXY_URL={proxy_url}")
                updated = True
            else:
                new_lines.append(line)
        if not updated:
            new_lines.append(f"PROXY_URL={proxy_url}")
        env_path.write_text("\n".join(new_lines) + "\n")

    click.echo(f"Proxy set: {proxy_url}")
    click.echo("All workers will use this proxy on their next job.")
    click.echo("Run 'scraper proxy test' to verify it is working against eBay.")


@proxy.command("test")
@click.option("--item-url", default="https://www.ebay.com/itm/397681222313",
              help="eBay item URL to test against")
def proxy_test(item_url: str) -> None:
    """Test the active proxy against a live eBay item page."""
    settings = Settings()
    redis_conn = get_redis(settings.redis_url)
    raw = redis_conn.get(PROXY_REDIS_KEY)
    proxy_url = raw.decode().strip() if raw else settings.proxy_url

    if not proxy_url:
        click.echo("No proxy configured. Run 'scraper proxy set <url>' first.")
        return

    masked = proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url
    click.echo(f"Testing proxy ...@{masked}")
    try:
        result = scrape_item(item_url, proxy_url=proxy_url)
    except ChallengeError:
        click.echo("FAIL  eBay served a bot-challenge page through this proxy.")
        click.echo("This is usually a datacenter IP. Use a rotating residential proxy.")
        return
    except Exception as exc:
        click.echo(f"FAIL  Could not reach eBay through the proxy: {exc}")
        click.echo("Check the proxy URL and that the proxy is reachable.")
        return
    if result:
        click.echo(f"OK  Proxy working. Scraped: {result.title[:60]}")
    else:
        click.echo("FAIL  eBay returned no product data for this item (it may be ended/removed).")
        click.echo("Try 'scraper proxy test --item-url <a live eBay item URL>'.")


@proxy.command("status")
def proxy_status() -> None:
    """Show the current proxy configuration."""
    settings = Settings()
    redis_conn = get_redis(settings.redis_url)
    raw = redis_conn.get(PROXY_REDIS_KEY)
    redis_proxy = raw.decode().strip() if raw else None

    if redis_proxy:
        masked = redis_proxy.split("@")[-1] if "@" in redis_proxy else redis_proxy
        click.echo(f"Active proxy (Redis, used by all workers): ...@{masked}")
    elif raw is not None:
        click.echo("Active proxy (Redis): cleared (workers make direct requests)")
    else:
        local = settings.proxy_url
        if local:
            masked = local.split("@")[-1] if "@" in local else local
            click.echo(f"Active proxy (local config fallback): ...@{masked}")
        else:
            click.echo("No proxy configured.")
    click.echo("\nUse 'scraper proxy set <url>' to configure.")
    click.echo("Use 'scraper proxy test' to verify against eBay.")


@proxy.command("clear")
def proxy_clear() -> None:
    """Remove the proxy. Workers will make direct requests."""
    settings = Settings()
    redis_conn = get_redis(settings.redis_url)
    # Store empty string (not delete) so Redis is authoritative over any local config
    redis_conn.set(PROXY_REDIS_KEY, "")

    env_path = _CONFIG_DIR / ".env"
    if env_path.exists():
        lines = env_path.read_text().splitlines()
        env_path.write_text("\n".join(
            "PROXY_URL=" if line.startswith("PROXY_URL=") else line
            for line in lines
        ) + "\n")

    click.echo("Proxy cleared. Workers will make direct requests on their next job.")


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--output", default="products.csv", show_default=True)
@click.option("--niche", default=None, help="Filter by niche tag (omit for all)")
def export(output: str, niche: str | None) -> None:
    """Export scraped products to a CSV file."""
    settings = Settings()

    async def _run() -> None:
        pool = await asyncpg.create_pool(settings.database_url)
        try:
            count = await export_to_csv(pool, output_path=output, niche=niche)
            click.echo(f"Exported {count} products to {output}")
        finally:
            await pool.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# db
# ---------------------------------------------------------------------------


@cli.group()
def db() -> None:
    """Database management (run on coordinator only)."""


@db.command("init")
def db_init() -> None:
    """Create the database schema."""
    settings = Settings()

    async def _run() -> None:
        pool = await asyncpg.create_pool(settings.database_url)
        try:
            await init_schema(pool)
            click.echo("Schema created.")
        finally:
            await pool.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# clear
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--niche", required=True, help="Niche tag to clear")
@click.confirmation_option(prompt="Remove all scraped data for this niche?")
def clear(niche: str) -> None:
    """Remove all scraped records for a niche."""
    settings = Settings()

    async def _run() -> None:
        pool = await asyncpg.create_pool(settings.database_url)
        try:
            await clear_niche(pool, niche)
            click.echo(f"Cleared niche: {niche}")
        finally:
            await pool.close()

    asyncio.run(_run())

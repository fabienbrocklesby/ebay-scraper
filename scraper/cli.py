import asyncio
import json
import logging
import shutil
import subprocess
import sys
import time
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any, Callable

import asyncpg
import click
from rq import Worker

from scraper.config import Settings
from scraper.db import (
    add_store, clear_niche, get_counts, init_schema,
    list_stores, remove_store, set_store_marketplace,
)
from scraper.export import export_to_csv, export_split_csv
from scraper.fetch import ChallengeError
from scraper.marketplace import detect_marketplace
from scraper.queue import (
    enqueue_items, get_queue, get_redis, queue_is_drained, resolve_proxy,
    resolve_isp_pool, normalize_proxy_url, PROXY_REDIS_KEY, ISP_POOL_REDIS_KEY,
    SCRAPED_SET_KEY,
)
from scraper.scraper import scrape_item
from scraper.store import (
    _normalize_store_url, extract_seller_id, get_item_urls_from_store,
    get_item_urls_via_unblocker,
)
from scraper.unblocker import fetch_via_unblocker, load_unblocker_config, UNBLOCKER_COUNT_KEY
from scraper.worker import start_worker

logger = logging.getLogger(__name__)

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
        f"  -e WORKER_CONCURRENCY={settings.worker_concurrency} \\\n"
        f"  -e MAX_RPS_PER_IP={settings.max_rps_per_ip} \\\n"
        f"  -e BATCH_SIZE={settings.batch_size} \\\n"
        f"  -e CHALLENGE_ESCALATION_THRESHOLD={settings.challenge_escalation_threshold} \\\n"
        f"  -e CHALLENGE_COOLDOWN_SECONDS={settings.challenge_cooldown_seconds} \\\n"
        f"  {image}"
    )


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


@cli.group()
def store() -> None:
    """Manage the list of stores to scrape."""


def _canonical_store_url(url: str) -> str:
    return _normalize_store_url(url)


def _parse_store_lines(text: str, default_niche: str | None) -> list[tuple[str, str]]:
    """Parse a bulk store file into (canonical_url, niche) pairs.

    Each non-blank, non-comment line is 'URL' or 'URL,niche'. A line's own niche
    wins; otherwise default_niche applies. Blank lines and lines starting with '#'
    are skipped. Raises click.ClickException if a line has no niche and no default.
    """
    entries: list[tuple[str, str]] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",", 1)]
        url = parts[0]
        niche = parts[1] if len(parts) > 1 and parts[1] else default_niche
        if niche is None:
            raise click.ClickException(
                f"Line {lineno}: no niche for '{url}'. Add ',<niche>' or pass --niche."
            )
        entries.append((_canonical_store_url(url), niche))
    return entries


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


@store.command("import")
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
@click.option("--niche", default=None, help="Default niche for lines that don't set their own.")
def store_import(file: str, niche: str | None) -> None:
    """Bulk-register stores from a text file.

    One store per line: 'URL' or 'URL,niche'. Blank lines and lines starting with
    '#' are ignored. Use --niche to set a default niche for lines without one.
    """
    settings = Settings()
    entries = _parse_store_lines(Path(file).read_text(), niche)
    if not entries:
        click.echo("No stores found in file.")
        return

    async def _run() -> None:
        pool = await asyncpg.create_pool(settings.database_url)
        try:
            for url, store_niche in entries:
                await add_store(pool, url, store_niche)
        finally:
            await pool.close()

    asyncio.run(_run())
    click.echo(f"Imported {len(entries)} stores.")


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


_FAILED_STORES_FILE = Path.home() / ".config" / "ebay-scraper" / "failed_stores.txt"


def _marketplace_seller_search(domain: str, store_slug: str) -> str:
    # Storefront, not /sch seller-search: eBay challenges seller-search from proxies
    # but serves /str storefront pages, so this is what the crawl can actually read.
    return f"https://{domain}/str/{store_slug}"


def _detect_and_resolve(
    seller_id: str,
    proxy_url: str | None,
    on_detected: Callable[[str, str], None] | None,
    unblocker_config: Any,
    redis_conn: Any,
) -> str | None:
    """Resolve a seller's home-marketplace storefront URL.

    Storefront pages aren't challenged like seller-search, so detection reads real
    item counts and adopts the best conclusive domain (see detect_marketplace for the
    priority tie-break). A domain that errored or was challenged is simply ignored, not
    a reason to abandon a clear winner. Only when no domain returned any items do we
    escalate to an unblocker (if configured) or return None so the store is flagged.
    Currency is recorded per item downstream, so this resolves for catalogue
    completeness rather than to guarantee a single currency.
    """
    outcome = detect_marketplace(seller_id, proxy_url)
    if outcome.result is not None:
        if on_detected is not None:
            on_detected(outcome.result.domain, outcome.result.country)
        return _marketplace_seller_search(outcome.result.domain, seller_id)

    if unblocker_config is not None and unblocker_config.enabled:
        def _ufetch(url, _proxy):
            html = fetch_via_unblocker(url, unblocker_config, redis_conn)
            if not html:
                raise ChallengeError("unblocker returned no content")
            return html
        uoutcome = detect_marketplace(seller_id, None, fetch_fn=_ufetch)
        if uoutcome.result is not None:
            if on_detected is not None:
                on_detected(uoutcome.result.domain, uoutcome.result.country)
            return _marketplace_seller_search(uoutcome.result.domain, seller_id)
    return None


def _discover_store(
    store_url: str,
    proxy_url: str | None,
    rps: float,
    cached: tuple[str, str] | None = None,
    on_detected: Callable[[str, str], None] | None = None,
    unblocker_config: Any = None,
    redis_conn: Any = None,
    max_pages: int = 9999,
    max_challenge_retries: int = 4,
) -> tuple[str, list[str]]:
    """Resolve a store's home marketplace, then crawl item URLs from that domain.

    eBay serves each seller's listings on their home marketplace (.com, .com.au,
    .co.uk, ...). A cached marketplace skips detection; otherwise detection is
    confidence-gated (see _detect_and_resolve): a proxy result is adopted only when no
    candidate was left undetermined, else we escalate to an unblocker or leave the
    store unresolved rather than scrape a cross-listing in the wrong currency. Returns
    (outcome, urls) with outcome in "ok", "empty", "blocked", or "unresolved": a crawl
    ChallengeError is "blocked", a clean crawl with no items is "empty", and a store
    whose home marketplace could not be determined is "unresolved".
    """
    seller_id = extract_seller_id(store_url)
    if cached is not None:
        resolved = _marketplace_seller_search(cached[0], seller_id)
    else:
        resolved = _detect_and_resolve(seller_id, proxy_url, on_detected, unblocker_config, redis_conn)
    if resolved is None:
        return ("unresolved", [])
    try:
        urls = get_item_urls_from_store(
            resolved, proxy_url=proxy_url, requests_per_second=rps, max_pages=max_pages,
            max_challenge_retries=max_challenge_retries,
        )
    except ChallengeError:
        # The cheap proxy is challenged on this seller's search surface (US/UK on a
        # flagged pool). Escalate discovery to the unblocker if configured; item
        # detail still goes through the proxy on the workers. No unblocker -> blocked.
        if unblocker_config is not None and unblocker_config.enabled:
            def _ufetch(url: str) -> str | None:
                return fetch_via_unblocker(url, unblocker_config, redis_conn)
            try:
                urls = get_item_urls_via_unblocker(resolved, _ufetch)
            except Exception:
                return ("blocked", [])
            return ("ok" if urls else "blocked", urls)
        return ("blocked", [])
    return ("ok" if urls else "empty", urls)


async def _load_cached_marketplaces(store_urls: list[str]) -> dict[str, tuple[str, str]]:
    settings = Settings()
    pool = await asyncpg.create_pool(settings.database_url)
    try:
        rows = await pool.fetch(
            """
            SELECT store_url, marketplace_domain, marketplace_country
              FROM stores
             WHERE store_url = ANY($1::text[]) AND marketplace_domain IS NOT NULL
            """,
            store_urls,
        )
        return {
            r["store_url"]: (r["marketplace_domain"], r["marketplace_country"]) for r in rows
        }
    finally:
        await pool.close()


async def _save_marketplace(store_url: str, domain: str, country: str) -> None:
    settings = Settings()
    pool = await asyncpg.create_pool(settings.database_url)
    try:
        await set_store_marketplace(pool, store_url, domain, country)
    finally:
        await pool.close()


def _persist(url: str, domain: str, country: str) -> None:
    try:
        asyncio.run(_save_marketplace(url, domain, country))
    except Exception as exc:  # caching the marketplace is best-effort; never abort discovery
        logger.warning("failed to persist marketplace for %s: %s", url, exc)


def _write_failed_stores(failures: list[tuple[str, str]]) -> None:
    _FAILED_STORES_FILE.parent.mkdir(parents=True, exist_ok=True)
    _FAILED_STORES_FILE.write_text("".join(f"{url},{niche}\n" for url, niche in failures))


def _run_discovery(stores: list[tuple[str, str]], proxy_url: str | None, settings: "Settings",
                   us_only: bool = False, max_pages: int = 9999,
                   discover_via_pool: bool = False):
    """Discover + queue a list of (store_url, niche). Returns (ok, empty, blocked, unresolved, total_queued).

    us_only pins every store to ebay.com (US) and skips home-marketplace detection.
    With a US-only item-fetch pool, non-US sellers can never be fetched anyway, so
    detecting and queuing them would only burn proxy reputation on doomed
    wrong-country fetches; pinning US makes those sellers return empty at discovery
    and never enter the queue. It also avoids the per-store multi-domain probes.

    discover_via_pool routes store pagination through the static ISP pool (a fresh
    pool IP per store, round-robin) instead of the rotating residential proxy. Use it
    when the residential proxy is degraded: the clean ISP IPs paginate reliably where
    a flagged residential pool returns nothing.
    """
    redis_conn = get_redis(settings.redis_url)
    queue = get_queue(redis_conn)
    unblocker_config = load_unblocker_config(redis_conn)
    pool = resolve_isp_pool(redis_conn) if discover_via_pool else []
    if discover_via_pool and not pool:
        raise click.ClickException(
            "--discover-via-pool needs a non-empty ISP pool (scraper proxy pool add <ip>)."
        )
    cached_map = asyncio.run(_load_cached_marketplaces([s[0] for s in stores]))
    ok: list[str] = []
    empty: list[tuple[str, str]] = []
    blocked: list[tuple[str, str]] = []
    unresolved: list[tuple[str, str]] = []
    total_queued = 0
    for i, (store_url, niche) in enumerate(stores):
        click.echo(f"  Crawling {store_url} ...")
        cached = ("www.ebay.com", "us") if us_only else cached_map.get(store_url)
        on_detected = lambda d, c, _u=store_url: _persist(_u, d, c)
        if pool:
            # Try the store across pool IPs (rotated start), failing fast per IP, so a
            # single flagged IP no longer blocks a store that another IP can crawl.
            outcome, item_urls = ("blocked", [])
            for k in range(len(pool)):
                proxy = pool[(i + k) % len(pool)]
                outcome, item_urls = _discover_store(
                    store_url, proxy, settings.requests_per_second, cached=cached,
                    on_detected=on_detected, unblocker_config=unblocker_config,
                    redis_conn=redis_conn, max_pages=max_pages, max_challenge_retries=1,
                )
                if outcome != "blocked":
                    break
        else:
            outcome, item_urls = _discover_store(
                store_url, proxy_url, settings.requests_per_second, cached=cached,
                on_detected=on_detected, unblocker_config=unblocker_config,
                redis_conn=redis_conn, max_pages=max_pages,
            )
        if outcome == "ok":
            queued = enqueue_items(queue, redis_conn, item_urls, niche=niche, store_url=store_url)
            total_queued += queued
            ok.append(store_url)
            click.echo(f"    OK: {len(item_urls)} items, {queued} new jobs queued  [{niche}]")
        elif outcome == "empty":
            empty.append((store_url, niche))
            click.echo(f"    0 results (empty store, or this IP was served a degraded view)  [{niche}]")
        elif outcome == "unresolved":
            unresolved.append((store_url, niche))
            click.echo(f"    home marketplace UNDETERMINED (proxy degraded, no unblocker)  [{niche}]")
        else:
            blocked.append((store_url, niche))
            click.echo(f"    BLOCKED by eBay  [{niche}]", err=True)
    return ok, empty, blocked, unresolved, total_queued


def _report_discovery(ok, empty, blocked, unresolved, total_queued: int) -> None:
    click.echo(
        f"\nDone. {len(ok)} stores OK, {len(empty)} returned 0 results, "
        f"{len(blocked)} blocked, {len(unresolved)} unresolved. "
        f"{total_queued} item jobs queued for workers."
    )
    if unresolved:
        click.echo(
            f"\n{len(unresolved)} store(s): home marketplace UNDETERMINED "
            f"(proxy degraded, no unblocker) - will retry"
        )
    failures = empty + blocked + unresolved
    if failures:
        _write_failed_stores(failures)
        click.echo(
            f"\n{len(failures)} store(s) returned nothing this pass and were saved to "
            f"{_FAILED_STORES_FILE}.\nThis is usually a flagged proxy IP or a hot "
            f"coordinator IP, not a real empty store. Re-run them later (a different "
            f"time gets fresh IPs) with:\n  scraper scrape retry"
        )


@scrape.command("start")
@click.option("--niche", default=None, help="Only scrape stores with this niche tag")
@click.option("--us-only", is_flag=True, default=False,
              help="Pin every store to ebay.com (US) and skip marketplace detection. "
                   "Use with a US-only ISP pool: non-US sellers can't be fetched anyway, "
                   "so this avoids burning proxy reputation on doomed wrong-country fetches.")
@click.option("--cap-per-store", type=int, default=0, show_default=True,
              help="Stop discovery after roughly this many items per store (0 = no cap). "
                   "Caps depth on giant stores and skips the slow price-partition crawl, "
                   "so a first full pass finishes sooner. eBay surfaces ~10000/store max.")
@click.option("--discover-via-pool", is_flag=True, default=False,
              help="Route store pagination through the ISP pool (a fresh pool IP per "
                   "store) instead of the rotating residential proxy. Use when the "
                   "residential proxy is degraded and returning challenges.")
def scrape_start(niche: str | None, us_only: bool, cap_per_store: int,
                 discover_via_pool: bool) -> None:
    """Paginate all registered stores and queue item scrape jobs for VPS workers.

    Store pagination runs here on the coordinator through the rotating proxy. Every
    store is reported as OK / 0-results / blocked, and anything that returned
    nothing is saved for 'scraper scrape retry'.
    """
    settings = Settings()

    async def _get_stores() -> list:
        pool = await asyncpg.create_pool(settings.database_url)
        try:
            return await list_stores(pool)
        finally:
            await pool.close()

    rows = asyncio.run(_get_stores())
    if niche:
        rows = [s for s in rows if s["niche"] == niche]
    if not rows:
        click.echo("No stores registered. Use 'scraper store add <url> --niche <tag>'")
        return

    redis_conn = get_redis(settings.redis_url)
    proxy_url = resolve_proxy(redis_conn, settings)

    stores = [(s["store_url"], s["niche"]) for s in rows]
    # eBay lists 240 items per page; max_pages bounds the per-store crawl to ~cap items.
    max_pages = max(1, cap_per_store // 240) if cap_per_store > 0 else 9999
    ok, empty, blocked, unresolved, total_queued = _run_discovery(
        stores, proxy_url, settings, us_only=us_only, max_pages=max_pages,
        discover_via_pool=discover_via_pool,
    )
    _report_discovery(ok, empty, blocked, unresolved, total_queued)


@scrape.command("retry")
@click.option("--us-only", is_flag=True, default=False,
              help="Pin every store to ebay.com (US) and skip marketplace detection.")
@click.option("--cap-per-store", type=int, default=0, show_default=True,
              help="Stop discovery after roughly this many items per store (0 = no cap).")
@click.option("--discover-via-pool", is_flag=True, default=False,
              help="Route store pagination through the ISP pool instead of residential.")
def scrape_retry(us_only: bool, cap_per_store: int, discover_via_pool: bool) -> None:
    """Re-run discovery for stores that returned 0 results or were blocked last time.

    Reads the failure list written by 'scrape start' and tries each store again
    (fresh IPs at a different time recover most of them). Stores that succeed are
    queued and dropped from the list; the rest stay for the next retry. Pass the same
    flags you used for 'scrape start' so retries crawl the same way.
    """
    settings = Settings()
    if not _FAILED_STORES_FILE.exists() or not _FAILED_STORES_FILE.read_text().strip():
        click.echo("No failed stores recorded. Nothing to retry.")
        return
    stores = _parse_store_lines(_FAILED_STORES_FILE.read_text(), None)

    redis_conn = get_redis(settings.redis_url)
    proxy_url = resolve_proxy(redis_conn, settings)

    max_pages = max(1, cap_per_store // 240) if cap_per_store > 0 else 9999
    ok, empty, blocked, unresolved, total_queued = _run_discovery(
        stores, proxy_url, settings, us_only=us_only, max_pages=max_pages,
        discover_via_pool=discover_via_pool,
    )
    _write_failed_stores(empty + blocked + unresolved)
    click.echo(
        f"\nRetry done. {len(ok)} recovered, {len(empty) + len(blocked) + len(unresolved)} still "
        f"failing (kept in {_FAILED_STORES_FILE}). {total_queued} item jobs queued."
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
    proxy_url = resolve_proxy(redis_conn, settings)

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
@click.option("--stores", "show_stores", is_flag=True, default=False,
              help="Also list every registered store (long).")
def scrape_status(show_stores: bool) -> None:
    """Show progress at a glance: how many items are done and how many to go.

    "Discovered" is every item URL found by the store crawl and queued for
    scraping. "Scraped" is what has landed in the database. Remaining is the gap.
    Run it on a loop to watch progress:  watch -n 30 scraper scrape status
    """
    from rq.registry import StartedJobRegistry, DeferredJobRegistry

    settings = Settings()
    redis_conn = get_redis(settings.redis_url)
    queue = get_queue(redis_conn)
    pool_size = len(resolve_isp_pool(redis_conn))
    discovered = int(redis_conn.scard(SCRAPED_SET_KEY) or 0)
    pending_batches = len(queue)
    in_flight = StartedJobRegistry(queue=queue).count
    awaiting_retry = DeferredJobRegistry(queue=queue).count

    async def _run() -> tuple:
        pool = await asyncpg.create_pool(settings.database_url)
        try:
            return await get_counts(pool), await list_stores(pool)
        finally:
            await pool.close()

    counts, stores = asyncio.run(_run())
    done = sum(counts.values())
    remaining = max(discovered - done, 0)
    pct = (done / discovered * 100) if discovered else 0.0

    click.echo("=== eBay Scraper status ===")
    click.echo(f"Stores registered:  {len(stores)}")
    click.echo(f"ISP pool IPs:       {pool_size}")
    click.echo("")
    click.echo(f"Items discovered:   {discovered:,}   (found by store crawl, queued to scrape)")
    click.echo(f"Items scraped:      {done:,}   ({pct:.1f}% done)")
    click.echo(f"Remaining:          {remaining:,}")
    click.echo("")
    click.echo(f"Work queue:         {pending_batches} batches pending, "
               f"{in_flight} in flight, {awaiting_retry} awaiting retry")
    if counts:
        click.echo("\nScraped per niche:")
        for n, c in sorted(counts.items()):
            click.echo(f"  {n}: {c:,}")
    if show_stores and stores:
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
    proxy_url = resolve_proxy(redis_conn, settings)

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


@proxy.group("pool")
def proxy_pool() -> None:
    """Manage the static ISP proxy pool used for item-detail fetching.

    Discovery (store pagination) uses the rotating proxy from 'scraper proxy set';
    the bulk item fetches are spread across this flat-rate ISP pool, one IP per
    request, each IP independently rate-limited. Add IPs as you buy them: workers
    pick up new members on their next batch, so the pool scales live with no restart.
    """


@proxy_pool.command("add")
@click.argument("proxy_url")
def proxy_pool_add(proxy_url: str) -> None:
    """Add an ISP proxy to the pool. Accepts IPRoyal's host:port:user:pass format."""
    try:
        normalized = normalize_proxy_url(proxy_url)
    except ValueError as exc:
        raise click.ClickException(str(exc))
    settings = Settings()
    redis_conn = get_redis(settings.redis_url)
    added = redis_conn.sadd(ISP_POOL_REDIS_KEY, normalized)
    pool = resolve_isp_pool(redis_conn)
    masked = normalized.split("@")[-1]
    if added:
        click.echo(f"Added ISP IP ...@{masked}. Pool now has {len(pool)} IP(s).")
    else:
        click.echo(f"ISP IP ...@{masked} already in pool ({len(pool)} IP(s)).")
    click.echo("Workers spread item fetches across the pool on their next batch.")


@proxy_pool.command("list")
def proxy_pool_list() -> None:
    """List the ISP IPs in the pool (passwords masked)."""
    settings = Settings()
    redis_conn = get_redis(settings.redis_url)
    pool = resolve_isp_pool(redis_conn)
    if not pool:
        click.echo("ISP pool is empty. Add IPs with 'scraper proxy pool add <host:port:user:pass>'.")
        return
    click.echo(f"ISP pool ({len(pool)} IP(s)):")
    for entry in pool:
        click.echo(f"  ...@{entry.split('@')[-1]}")


@proxy_pool.command("remove")
@click.argument("proxy_url")
def proxy_pool_remove(proxy_url: str) -> None:
    """Remove an ISP proxy from the pool."""
    try:
        normalized = normalize_proxy_url(proxy_url)
    except ValueError as exc:
        raise click.ClickException(str(exc))
    settings = Settings()
    redis_conn = get_redis(settings.redis_url)
    removed = redis_conn.srem(ISP_POOL_REDIS_KEY, normalized)
    pool = resolve_isp_pool(redis_conn)
    if removed:
        click.echo(f"Removed. Pool now has {len(pool)} IP(s).")
    else:
        click.echo(f"Not found in pool ({len(pool)} IP(s)). 'scraper proxy pool list' to see members.")


@proxy_pool.command("clear")
def proxy_pool_clear() -> None:
    """Empty the ISP pool. Item fetches fall back to the box/residential path."""
    settings = Settings()
    redis_conn = get_redis(settings.redis_url)
    redis_conn.delete(ISP_POOL_REDIS_KEY)
    click.echo("ISP pool cleared.")


@proxy_pool.command("test")
@click.option("--item-url", default="https://www.ebay.com/itm/397681222313",
              help="eBay item URL to test each pool IP against")
def proxy_pool_test(item_url: str) -> None:
    """Fetch a live eBay item through each pool IP and report OK / blocked per IP."""
    settings = Settings()
    redis_conn = get_redis(settings.redis_url)
    pool = resolve_isp_pool(redis_conn)
    if not pool:
        click.echo("ISP pool is empty. Add IPs with 'scraper proxy pool add <...>'.")
        return
    click.echo(f"Testing {len(pool)} pool IP(s) against {item_url} ...")
    ok = 0
    for entry in pool:
        masked = entry.split("@")[-1]
        try:
            result = scrape_item(item_url, proxy_url=entry)
        except ChallengeError:
            click.echo(f"  BLOCK  ...@{masked}  (challenge / 403)")
            continue
        except Exception as exc:
            click.echo(f"  ERR    ...@{masked}  {exc}")
            continue
        if result:
            ok += 1
            click.echo(f"  OK     ...@{masked}  {result.title[:40]}")
        else:
            click.echo(f"  EMPTY  ...@{masked}  (no product data; item may be ended)")
    click.echo(f"\n{ok}/{len(pool)} pool IP(s) OK.")


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
# run (end-to-end orchestrator)
# ---------------------------------------------------------------------------


async def _import_stores(stores: list[tuple[str, str]]) -> None:
    settings = Settings()
    pool = await asyncpg.create_pool(settings.database_url)
    try:
        for store_url, niche in stores:
            await add_store(pool, _canonical_store_url(store_url), niche)
    finally:
        await pool.close()


async def _export_split_csv_all(export_dir: str, rows_per_file: int) -> tuple[list[str], int]:
    settings = Settings()
    pool = await asyncpg.create_pool(settings.database_url)
    try:
        paths = await export_split_csv(pool, export_dir, rows_per_file=rows_per_file, niche=None)
        # Mirror the exact filter used by export_split_csv / get_products_by_niche(None):
        # no niche and no is_active filter, so this COUNT matches the CSV row count exactly.
        total_rows = await pool.fetchval("SELECT COUNT(*) FROM products") or 0
        return paths, int(total_rows)
    finally:
        await pool.close()


def _wait_for_drain(queue) -> None:
    """Block until the queue drains. Abort if no worker is connected across two
    consecutive checks, so a missing/dead worker fails loudly instead of hanging forever."""
    no_worker_strikes = 0
    while not queue_is_drained(queue):
        if not Worker.all(queue=queue):
            no_worker_strikes += 1
            if no_worker_strikes >= 2:
                raise click.ClickException(
                    "No workers connected; the queue cannot drain. Start a worker "
                    "(scraper worker start) and re-run, or use --no-wait."
                )
        else:
            no_worker_strikes = 0
        time.sleep(10)
        click.echo(f"  pending={queue.count} ...")


@cli.command()
@click.argument("store_file", type=click.Path(exists=True))
@click.option("--export-dir", default="exports", help="Directory for the output CSV files.")
@click.option("--rows-per-file", default=500_000, type=int, help="Max rows per CSV file.")
@click.option("--no-wait", is_flag=True, help="Queue scraping and exit without waiting/exporting.")
def run(store_file: str, export_dir: str, rows_per_file: int, no_wait: bool) -> None:
    """Import a store-URL file, scrape every product across all marketplaces, write CSVs."""
    settings = Settings()
    text = Path(store_file).read_text()
    stores = _parse_store_lines(text, default_niche="")
    click.echo(f"Loaded {len(stores)} stores from {store_file}")

    asyncio.run(_import_stores(stores))

    redis_conn = get_redis(settings.redis_url)
    proxy_url = resolve_proxy(redis_conn, settings)

    ok, empty, blocked, unresolved, total_queued = _run_discovery(stores, proxy_url, settings)
    _report_discovery(ok, empty, blocked, unresolved, total_queued)

    if no_wait:
        click.echo("Queued. Exiting without waiting (--no-wait).")
        return

    queue = get_queue(redis_conn)
    click.echo("Scraping... (waiting for the queue to drain)")
    if not Worker.all(queue=queue):
        click.echo(
            "Warning: no workers connected. Start a worker (scraper worker start) "
            "in another terminal/VPS, or this will wait indefinitely."
        )
    _wait_for_drain(queue)

    from rq.registry import FailedJobRegistry
    paths, total_rows = asyncio.run(_export_split_csv_all(export_dir, rows_per_file))
    unblocker_used = int(redis_conn.get(UNBLOCKER_COUNT_KEY) or 0)
    failed_jobs = FailedJobRegistry(queue=queue).count
    click.echo(
        f"Done. {len(ok)}/{len(stores)} stores OK, {total_rows} products exported, "
        f"{failed_jobs} failed jobs, {unblocker_used} unblocker requests, "
        f"{len(paths)} CSV files in {export_dir}/"
    )


# ---------------------------------------------------------------------------
# setup wizard
# ---------------------------------------------------------------------------


def _redact_url(url: str) -> str:
    """Mask any embedded password in a URL before display (user:pass@host -> user:***@host)."""
    import re
    return re.sub(r"://([^:/@]+):[^@]+@", r"://\1:***@", url)


_PROXY_PROBE_MARKETS = {
    "US": "https://www.ebay.com/sch/i.html?_nkw=phone&_ipg=60",
    "AU": "https://www.ebay.com.au/sch/i.html?_nkw=phone&_ipg=60",
    "UK": "https://www.ebay.co.uk/sch/i.html?_nkw=phone&_ipg=60",
}


def _probe_proxy_markets(proxy_url: str) -> dict[str, bool]:
    """Probe the proxy against each marketplace; return {country: clean_grid_returned}.

    A residential pool can be challenge-flagged by eBay for one country while clean for
    another. The scraper covers flagged countries via the unblocker, so this reports each
    country separately rather than a single pass/fail.
    """
    from scraper.fetch import apply_proxy_country, build_session, is_challenge_page
    from scraper.store import _extract_item_urls

    results: dict[str, bool] = {}
    for country, url in _PROXY_PROBE_MARKETS.items():
        try:
            session = build_session(apply_proxy_country(proxy_url, url))
            session.get(url.split("/sch")[0] + "/", timeout=30)
            resp = session.get(url, timeout=40)
            results[country] = (not is_challenge_page(resp.text)) and bool(
                _extract_item_urls(resp.text)
            )
        except Exception:  # noqa: BLE001
            results[country] = False
    return results


def _probe_proxy_ok(proxy_url: str) -> bool:
    """True if the proxy can fetch a clean eBay grid on at least one marketplace.

    A proxy flagged for some countries but clean for others is still usable: the scraper
    routes flagged countries through the unblocker. Only a proxy that fails ALL three
    marketplaces (or is unreachable) is unusable.
    """
    return any(_probe_proxy_markets(proxy_url).values())


def _probe_unblocker_ok(username: str, password: str) -> bool:
    """Fetch a real eBay page via the Oxylabs unblocker; return True if HTML is returned.

    The broad except is intentional: any failure means the unblocker is not usable, and
    the wizard must report a clean failure rather than crash.
    """
    from scraper.unblocker import UnblockerConfig, fetch_via_unblocker
    try:
        cfg = UnblockerConfig(provider="oxylabs", username=username, password=password)
        html = fetch_via_unblocker(
            "https://www.ebay.com/sch/i.html?_ssn=onlinesound&_pgn=1&_ipg=60", cfg
        )
        return bool(html)
    except Exception:  # noqa: BLE001
        return False


@cli.command()
def setup() -> None:
    """Interactive setup: paste proxy and optional unblocker credentials."""
    settings = Settings()
    redis_conn = get_redis(settings.redis_url)

    proxy_url = click.prompt("Residential proxy URL (http://user:pass@host:port)")
    click.echo("  testing proxy against eBay (US / AU / UK) ...")
    markets = _probe_proxy_markets(proxy_url)
    for country, ok in markets.items():
        click.echo(f"    {country}: {'clean' if ok else 'flagged (will use the unblocker)'}")
    clean = [c for c, ok in markets.items() if ok]
    if not clean:
        click.echo(
            "  proxy FAILED on every marketplace (unreachable, out of credit, or fully "
            "flagged). Not saved. Check the proxy and re-run setup."
        )
        return
    redis_conn.set(PROXY_REDIS_KEY, proxy_url)
    click.echo(f"  proxy saved. Clean for: {', '.join(clean)}.")
    all_clean = len(clean) == len(markets)
    if not all_clean:
        click.echo(
            "  NOTE: some marketplaces are flagged on this proxy. Add the Oxylabs "
            "unblocker below so stores on those marketplaces still get found."
        )

    if click.confirm(
        "Add the Oxylabs unblocker (recommended)?", default=not all_clean
    ):
        user = click.prompt("Oxylabs username")
        pw = click.prompt("Oxylabs password", hide_input=True)
        click.echo("  testing unblocker ...")
        if _probe_unblocker_ok(user, pw):
            from scraper.unblocker import (
                UNBLOCKER_PROVIDER_KEY, OXYLABS_USER_KEY, OXYLABS_PASS_KEY,
            )
            redis_conn.set(UNBLOCKER_PROVIDER_KEY, "oxylabs")
            redis_conn.set(OXYLABS_USER_KEY, user)
            redis_conn.set(OXYLABS_PASS_KEY, pw)
            click.echo("  unblocker OK and saved.")
        else:
            click.echo("  unblocker test FAILED. Skipped (proxy-only is fine).")

    click.echo("Setup complete. Run `scraper doctor` to confirm everything is green.")


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


async def _ping_db() -> None:
    settings = Settings()
    pool = await asyncpg.create_pool(settings.database_url)
    try:
        await pool.fetchval("SELECT 1")
    finally:
        await pool.close()


@cli.command()
def doctor() -> None:
    """Check Redis, Postgres, proxy, unblocker, and connected workers."""
    settings = Settings()

    def line(label: str, ok: bool, detail: str = "") -> None:
        mark = "OK " if ok else "XX "
        click.echo(f"[{mark}] {label}{(' - ' + detail) if detail else ''}")

    try:
        redis_conn = get_redis(settings.redis_url)
        redis_conn.ping()
        line("Redis", True, _redact_url(settings.redis_url))
    except Exception as exc:
        line("Redis", False, str(exc))
        return

    try:
        asyncio.run(_ping_db())
        line("Postgres", True, _redact_url(settings.database_url))
    except Exception as exc:
        line("Postgres", False, str(exc))

    proxy_url = resolve_proxy(redis_conn, settings)
    if proxy_url:
        markets = _probe_proxy_markets(proxy_url)
        clean = [c for c, ok in markets.items() if ok]
        detail = "clean: " + (", ".join(clean) if clean else "none") + (
            "  (flagged: " + ", ".join(c for c, ok in markets.items() if not ok) + ", covered by unblocker)"
            if clean and len(clean) < len(markets) else ""
        )
        line("Proxy", bool(clean), detail)
    else:
        line("Proxy", False, "not configured (run `scraper setup`)")

    cfg = load_unblocker_config(redis_conn)
    if cfg.enabled:
        used = int(redis_conn.get(UNBLOCKER_COUNT_KEY) or 0)
        line("Unblocker", True, f"oxylabs configured, {used} requests used")
    else:
        line("Unblocker", True, "off (proxy-only) - this is fine")

    queue = get_queue(redis_conn)
    worker_count = len(Worker.all(queue=queue))
    line("Workers", worker_count > 0, f"{worker_count} connected")


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

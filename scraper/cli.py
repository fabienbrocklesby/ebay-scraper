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
from scraper.db import clear_niche, get_counts, init_schema
from scraper.export import export_to_csv
from scraper.queue import enqueue_items, get_queue, get_redis
from scraper.store import get_item_urls_from_store
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


def _get_tailscale_ip() -> str | None:
    result = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _tailscale_connected() -> bool:
    if not _is_command_available("tailscale"):
        return False
    result = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True)
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
    click.echo("Run 'scraper status' on the coordinator to confirm it is pulling jobs.")


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
# add
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("source")
@click.option("--niche", required=True, help="Tag for this batch (e.g. car-accessories)")
def add(source: str, niche: str) -> None:
    """Add a store URL or text file of URLs to the scrape queue."""
    settings = Settings()

    if source.startswith("http"):
        store_urls = [source]
    else:
        with open(source) as f:
            store_urls = [line.strip() for line in f if line.strip()]

    redis_conn = get_redis(settings.redis_url)
    queue = get_queue(redis_conn)
    total = 0

    for store_url in store_urls:
        click.echo(f"Fetching item URLs from {store_url} ...")
        item_urls = get_item_urls_from_store(
            store_url,
            proxy_url=settings.proxy_url,
            requests_per_second=settings.requests_per_second,
        )
        count = enqueue_items(queue, redis_conn, item_urls, niche=niche, store_url=store_url)
        click.echo(f"  {count} new items queued ({len(item_urls) - count} already seen)")
        total += count

    click.echo(f"Done. {total} total items added to queue.")


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
# status
# ---------------------------------------------------------------------------


@cli.command()
def status() -> None:
    """Show queue depth and per-niche scraped counts."""
    settings = Settings()
    redis_conn = get_redis(settings.redis_url)
    queue = get_queue(redis_conn)

    async def _run() -> None:
        pool = await asyncpg.create_pool(settings.database_url)
        try:
            counts = await get_counts(pool)
            total = sum(counts.values())
            click.echo(f"Queued jobs:   {len(queue)}")
            click.echo(f"Total scraped: {total}")
            if counts:
                click.echo("\nPer-niche:")
                for n, c in sorted(counts.items()):
                    click.echo(f"  {n}: {c}")
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

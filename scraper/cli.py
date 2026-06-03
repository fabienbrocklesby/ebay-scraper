import asyncio
import subprocess
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


@click.group()
def cli() -> None:
    """eBay scraper: queue stores, run workers, export results."""


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
    click.echo("")
    click.echo("Next: run 'scraper connect <this-machine-ip-or-tailscale-host>' on each worker.")


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


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("host")
@click.option("--db-password", default="scraper", show_default=True, help="Postgres password")
@click.option("--db-user", default="scraper", show_default=True, help="Postgres user")
@click.option("--db-name", default="ebayscraper", show_default=True, help="Postgres database")
@click.option("--redis-port", default=6379, show_default=True)
@click.option("--db-port", default=5432, show_default=True)
@click.option("--proxy", default="", help="Rotating residential proxy URL (http://user:pass@host:port)")
@click.option("--rate", default=0.5, show_default=True, help="Requests per second")
def connect(
    host: str,
    db_password: str,
    db_user: str,
    db_name: str,
    redis_port: int,
    db_port: int,
    proxy: str,
    rate: float,
) -> None:
    """Configure this machine to connect to a coordinator.

    HOST is the coordinator's IP address or Tailscale hostname.
    """
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

    click.echo(f"Config written to {env_path}")
    click.echo(f"  Redis:    {redis_url}")
    click.echo(f"  Postgres: {db_url}")
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
@click.option("--image", default="ebay-scraper-worker", show_default=True, help="Docker image name")
def worker_docker_run(image: str) -> None:
    """Print the docker run command for running a worker on any machine.

    Build the image first:  docker build -t ebay-scraper-worker <repo-path>
    """
    settings = Settings()
    proxy = settings.proxy_url or ""
    click.echo(
        f"docker run -d --restart=always \\\n"
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
@click.option("--output", default="products.csv", show_default=True, help="Output file path")
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

import asyncio

import asyncpg
import click

from scraper.config import Settings
from scraper.db import clear_niche, get_counts, init_schema
from scraper.export import export_to_csv
from scraper.queue import enqueue_items, get_queue, get_redis
from scraper.store import get_item_ids_from_store
from scraper.worker import start_worker


@click.group()
def cli() -> None:
    """eBay scraper: queue stores, run workers, export results."""


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
        click.echo(f"Fetching item IDs from {store_url} ...")
        item_ids = get_item_ids_from_store(
            store_url,
            proxy_url=settings.proxy_url,
            requests_per_second=settings.requests_per_second,
        )
        count = enqueue_items(queue, redis_conn, item_ids, niche=niche, store_url=store_url)
        click.echo(f"  {count} new items queued ({len(item_ids) - count} already seen)")
        total += count

    click.echo(f"Done. {total} total items added to queue.")


@cli.group()
def worker() -> None:
    """Worker commands."""


@worker.command("start")
def worker_start() -> None:
    """Start a scrape worker on this VPS (runs until Ctrl+C)."""
    settings = Settings()
    click.echo("Starting worker. Press Ctrl+C to stop.")
    start_worker(settings.redis_url)


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

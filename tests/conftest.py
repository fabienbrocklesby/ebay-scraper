import os

import asyncpg
import pytest

TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql://scraper:scraper@localhost:5432/ebayscraper_test",
)


@pytest.fixture(scope="session")
async def db_pool():
    from scraper.db import init_schema
    pool = await asyncpg.create_pool(TEST_DB_URL)
    await init_schema(pool)
    yield pool
    await pool.close()


@pytest.fixture(autouse=True)
async def clean_products(db_pool):
    yield
    await db_pool.execute("DELETE FROM products")

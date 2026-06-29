"""Session fixtures for postgres and mongodb integration tests."""

import os

import asyncpg
import pytest
from motor.motor_asyncio import AsyncIOMotorClient
from testcontainers.mongodb import MongoDbContainer
from testcontainers.postgres import PostgresContainer

# On macOS the Ryuk sidecar container (testcontainers' cleanup daemon) occasionally
# fails to expose its port in time, causing a flaky ConnectionError on the first run.
# Docker Desktop cleans up containers itself, so Ryuk is not needed on macOS.
if getattr(os, "uname", None) and os.uname().sysname == "Darwin":
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "pg_pool" in item.fixturenames or "mongo_db" in item.fixturenames:
            item.add_marker(pytest.mark.docker)


@pytest.fixture(scope="session")
async def pg_pool():
    with PostgresContainer("postgres:16") as postgres:
        url = postgres.get_connection_url().replace("+psycopg2", "")
        pool = await asyncpg.create_pool(url)
        yield pool
        await pool.close()


@pytest.fixture(scope="session")
async def mongo_db():
    with MongoDbContainer("mongo:7") as mongo:
        client = AsyncIOMotorClient(mongo.get_connection_url())
        await client.server_info()
        yield client["llmbroker_test"]
        client.close()

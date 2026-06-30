"""Session fixtures for postgres and mongodb integration tests."""

import os

import asyncpg
import pytest
from motor.motor_asyncio import AsyncIOMotorClient
from testcontainers.mongodb import MongoDbContainer
from testcontainers.postgres import PostgresContainer

import llmbroker.mongodb
import llmbroker.postgres
import llmbroker.sqlite
from llmbroker.standalone.telemetry import NoTelemetry

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


@pytest.fixture(
    params=["toml", "sqlite", "postgres", "mongodb"],
    ids=["toml", "sqlite", "postgres", "mongodb"],
)
async def any_telemetry(request, tmp_path_factory, pg_pool, mongo_db):
    """Telemetry backend parametrized over every implemented storage layer.

    postgres and mongodb variants are marked docker automatically (pg_pool/mongo_db
    appear in fixturenames → pytest_collection_modifyitems picks them up).
    The toml and sqlite variants do not use the DB connections.
    """
    param = request.param

    if param == "toml":
        yield NoTelemetry()

    elif param == "sqlite":
        db_path = str(tmp_path_factory.mktemp("any_tel_sqlite") / "tel.db")
        yield llmbroker.sqlite.Telemetry(db_path)

    elif param == "postgres":
        yield llmbroker.postgres.Telemetry(pg_pool)
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM llmbroker_calls")

    elif param == "mongodb":
        yield llmbroker.mongodb.Telemetry(mongo_db)
        await mongo_db["llmbroker_calls"].delete_many({})


@pytest.fixture(
    params=["sqlite", "postgres", "mongodb"],
    ids=["sqlite", "postgres", "mongodb"],
)
async def queryable_telemetry(request, tmp_path_factory, pg_pool, mongo_db):
    """Queryable telemetry backends only — those that implement metrics() for warm-start seeding."""
    param = request.param

    if param == "sqlite":
        db_path = str(tmp_path_factory.mktemp("q_tel_sqlite") / "tel.db")
        yield llmbroker.sqlite.Telemetry(db_path)

    elif param == "postgres":
        yield llmbroker.postgres.Telemetry(pg_pool)
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM llmbroker_calls")

    elif param == "mongodb":
        yield llmbroker.mongodb.Telemetry(mongo_db)
        await mongo_db["llmbroker_calls"].delete_many({})

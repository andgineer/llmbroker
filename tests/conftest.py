"""Session fixtures for postgres and mongodb integration tests."""

import contextlib
import os
from dataclasses import dataclass

import asyncpg
import fakeredis.aioredis
import pytest
from motor.motor_asyncio import AsyncIOMotorClient
from testcontainers.mongodb import MongoDbContainer
from testcontainers.postgres import PostgresContainer

import llmbroker.mongodb
import llmbroker.postgres
import llmbroker.redis
import llmbroker.sqlite
from llmbroker.broker import AsyncBroker
from llmbroker.protocols.registry import MutableRegistryProtocol, RegistryProtocol
from llmbroker.protocols.secrets import SecretsProtocol
from llmbroker.protocols.state_store import StateStoreProtocol
from llmbroker.protocols.telemetry import TelemetryProtocol
from llmbroker.standalone.registry import Registry as TomlRegistry
from llmbroker.standalone.secrets import DictSecrets, Secrets
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


# ---------------------------------------------------------------------------
# Stack dataclass — curated full-stack descriptor for broker E2E tests
# ---------------------------------------------------------------------------


@dataclass
class Stack:
    """All four ports wired together; make_broker() constructs an AsyncBroker over them."""

    name: str
    registry: RegistryProtocol
    secrets: SecretsProtocol
    state_store: StateStoreProtocol | None
    telemetry: TelemetryProtocol
    queryable: bool
    persistent: bool

    def make_broker(self, **kw) -> AsyncBroker:
        return AsyncBroker(
            self.registry,
            secrets=self.secrets,
            state_store=self.state_store,
            telemetry=self.telemetry,
            **kw,
        )


# ---------------------------------------------------------------------------
# A1 — Per-port fixtures (parametrized over each port's backends)
# ---------------------------------------------------------------------------


@pytest.fixture(
    params=["toml", "sqlite", "postgres", "mongodb"],
    ids=["toml", "sqlite", "postgres", "mongodb"],
)
async def any_registry(
    request, tmp_path_factory, pg_pool, mongo_db
) -> MutableRegistryProtocol | RegistryProtocol:
    """Registry backend parametrized over every implemented storage layer."""
    param = request.param
    if param == "toml":
        p = tmp_path_factory.mktemp("any_reg_toml") / "llms.toml"
        p.write_text("")
        yield TomlRegistry(p)
    elif param == "sqlite":
        db_path = str(tmp_path_factory.mktemp("any_reg_sqlite") / "reg.db")
        yield llmbroker.sqlite.Registry(db_path)
    elif param == "postgres":
        yield llmbroker.postgres.Registry(pg_pool)
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM llmbroker_registry")
    elif param == "mongodb":
        yield llmbroker.mongodb.Registry(mongo_db)
        await mongo_db["llmbroker_registry"].delete_many({})


@pytest.fixture(
    params=["env", "sqlite", "postgres", "mongodb"],
    ids=["env", "sqlite", "postgres", "mongodb"],
)
async def any_secrets(request, tmp_path_factory, pg_pool, mongo_db) -> SecretsProtocol:
    """Secrets backend parametrized over every implemented storage layer."""
    param = request.param
    if param == "env":
        yield Secrets()
    elif param == "sqlite":
        db_path = str(tmp_path_factory.mktemp("any_sec_sqlite") / "sec.db")
        yield llmbroker.sqlite.Secrets(db_path)
    elif param == "postgres":
        yield llmbroker.postgres.Secrets(pg_pool)
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM llmbroker_secrets")
    elif param == "mongodb":
        yield llmbroker.mongodb.Secrets(mongo_db)
        await mongo_db["llmbroker_secrets"].delete_many({})


@pytest.fixture(
    params=["sqlite", "postgres", "mongodb", "redis"],
    ids=["sqlite", "postgres", "mongodb", "redis"],
)
async def any_state_store(request, tmp_path_factory, pg_pool, mongo_db) -> StateStoreProtocol:
    """State-store backend parametrized over every implemented storage layer."""
    param = request.param
    if param == "sqlite":
        db_path = str(tmp_path_factory.mktemp("any_ss_sqlite") / "ss.db")
        yield llmbroker.sqlite.StateStore(db_path)
    elif param == "postgres":
        yield llmbroker.postgres.StateStore(pg_pool)
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM llmbroker_state")
    elif param == "mongodb":
        yield llmbroker.mongodb.StateStore(mongo_db)
        await mongo_db["llmbroker_state"].delete_many({})
    elif param == "redis":
        client = fakeredis.aioredis.FakeRedis(decode_responses=True)
        yield llmbroker.redis.StateStore(client)


# ---------------------------------------------------------------------------
# A2 — Stack factory fixture (broker E2E)
# ---------------------------------------------------------------------------

_ALL_STACKS = ["all_sqlite", "all_postgres", "all_mongodb", "scaled", "minimal"]
_PERSISTENT_STACKS = ["all_sqlite", "all_postgres", "all_mongodb", "scaled"]

_PG_TABLES = ("llmbroker_registry", "llmbroker_calls", "llmbroker_secrets", "llmbroker_state")
_MONGO_COLLS = ("llmbroker_registry", "llmbroker_calls", "llmbroker_secrets", "llmbroker_state")


@contextlib.asynccontextmanager
async def _stack_ctx(name: str, tmp_path_factory, pg_pool, mongo_db):
    if name == "all_sqlite":
        base = str(tmp_path_factory.mktemp("stack_sqlite"))
        yield Stack(
            name="all_sqlite",
            registry=llmbroker.sqlite.Registry(f"{base}/llms.db"),
            secrets=llmbroker.sqlite.Secrets(f"{base}/llms.db"),
            state_store=llmbroker.sqlite.StateStore(f"{base}/llms.db"),
            telemetry=llmbroker.sqlite.Telemetry(f"{base}/llms.db"),
            queryable=True,
            persistent=True,
        )

    elif name == "all_postgres":
        s = Stack(
            name="all_postgres",
            registry=llmbroker.postgres.Registry(pg_pool),
            secrets=llmbroker.postgres.Secrets(pg_pool),
            state_store=llmbroker.postgres.StateStore(pg_pool),
            telemetry=llmbroker.postgres.Telemetry(pg_pool),
            queryable=True,
            persistent=True,
        )
        try:
            yield s
        finally:
            async with pg_pool.acquire() as conn:
                for table in _PG_TABLES:
                    await conn.execute(f"DELETE FROM {table}")  # noqa: S608

    elif name == "all_mongodb":
        s = Stack(
            name="all_mongodb",
            registry=llmbroker.mongodb.Registry(mongo_db),
            secrets=llmbroker.mongodb.Secrets(mongo_db),
            state_store=llmbroker.mongodb.StateStore(mongo_db),
            telemetry=llmbroker.mongodb.Telemetry(mongo_db),
            queryable=True,
            persistent=True,
        )
        try:
            yield s
        finally:
            for coll in _MONGO_COLLS:
                await mongo_db[coll].delete_many({})

    elif name == "scaled":
        redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
        s = Stack(
            name="scaled",
            registry=llmbroker.postgres.Registry(pg_pool),
            secrets=DictSecrets({}),
            state_store=llmbroker.redis.StateStore(redis_client),
            telemetry=llmbroker.postgres.Telemetry(pg_pool),
            queryable=True,
            persistent=True,
        )
        try:
            yield s
        finally:
            async with pg_pool.acquire() as conn:
                await conn.execute("DELETE FROM llmbroker_registry")
                await conn.execute("DELETE FROM llmbroker_calls")

    elif name == "minimal":
        toml_path = tmp_path_factory.mktemp("stack_minimal") / "llms.toml"
        toml_path.write_text("")
        yield Stack(
            name="minimal",
            registry=TomlRegistry(toml_path),
            secrets=Secrets(),
            state_store=None,
            telemetry=NoTelemetry(),
            queryable=False,
            persistent=False,
        )


@pytest.fixture(params=_ALL_STACKS, ids=_ALL_STACKS)
async def stack(request, tmp_path_factory, pg_pool, mongo_db) -> Stack:
    """Full-stack fixture over all five curated deployment shapes."""
    async with _stack_ctx(request.param, tmp_path_factory, pg_pool, mongo_db) as s:
        yield s


@pytest.fixture(params=_PERSISTENT_STACKS, ids=_PERSISTENT_STACKS)
async def persistent_stack(request, tmp_path_factory, pg_pool, mongo_db) -> Stack:
    """Full-stack fixture restricted to stacks with a real state_store and queryable telemetry."""
    async with _stack_ctx(request.param, tmp_path_factory, pg_pool, mongo_db) as s:
        yield s

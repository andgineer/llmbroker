"""Tests for the source-parameter dispatch on AsyncBroker/Broker (replaces ``stack=``)."""

import asyncio
import sys

import llmbroker.sqlite
import pytest

from llmbroker.backends.ports import StoreKnowledge, StoreRegistry, StoreSecrets
from llmbroker.broker import AsyncBroker
from llmbroker.broker.source import resolve_source
from llmbroker.models import LLMConfig
from llmbroker.mongodb.driver import MongoDriver
from llmbroker.postgres.driver import PostgresDriver
from llmbroker.sqlite.driver import SqliteDriver
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.sync import Broker


def _cfg(name: str = "llm1", api_key_ref: str = "KEY") -> LLMConfig:
    return LLMConfig(name=name, base_url="https://x/v1", model="m", api_key_ref=api_key_ref)


async def test_sqlite_source_wires_three_ports(tmp_path):
    db_path = str(tmp_path / "broker.db")
    registry, secrets, knowledge = resolve_source(db_path)
    assert isinstance(registry, StoreRegistry)
    assert isinstance(secrets, StoreSecrets)
    assert isinstance(knowledge, StoreKnowledge)

    await registry.mirror([_cfg()])
    await secrets.set("KEY", "secret-value")

    async with AsyncBroker(db_path) as broker:
        assert await broker.count() == 1
        assert broker._pool.resolved_key("llm1") == "secret-value"


def test_sqlite_source_dot_sqlite_suffix_and_url_form_both_dispatch(tmp_path):
    for path in (str(tmp_path / "a.sqlite"), f"sqlite://{tmp_path / 'b.db'}"):
        registry, _secrets, _knowledge = resolve_source(path)
        assert isinstance(registry, StoreRegistry)
        assert isinstance(registry._driver, SqliteDriver)  # noqa: SLF001


def test_toml_source_dispatches_to_file_registry_with_env_secrets_default(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text('[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n')

    async def run():
        async with AsyncBroker(str(f)) as broker:
            assert await broker.count() == 1

    asyncio.run(run())


def test_unrecognized_source_raises_clear_error():
    with pytest.raises(ValueError, match="unrecognized registry source"):
        resolve_source("not-a-known-form")


def test_no_registry_raises():
    with pytest.raises(ValueError, match="requires a `registry` source"):
        AsyncBroker()


def test_sync_broker_no_registry_raises():
    with pytest.raises(ValueError, match="requires a `registry` source"):
        Broker()


async def test_explicit_secrets_override_wins_over_sqlite_source(tmp_path):
    db_path = str(tmp_path / "broker.db")
    await llmbroker.sqlite.Registry(db_path).mirror([_cfg()])
    await llmbroker.sqlite.Secrets(db_path).set("KEY", "from-sqlite")
    override_secrets = DictSecrets({"KEY": "from-override"})

    async with AsyncBroker(db_path, secrets=override_secrets) as broker:
        assert broker._pool.resolved_key("llm1") == "from-override"


def test_postgres_source_dispatches_to_postgres_ports_lazily():
    """No live connection is touched — pool creation is deferred to first ``ensure_schema()``."""
    registry, secrets, knowledge = resolve_source("postgresql://localhost/db")
    assert isinstance(registry, StoreRegistry)
    assert isinstance(secrets, StoreSecrets)
    assert isinstance(knowledge, StoreKnowledge)
    assert isinstance(registry._driver, PostgresDriver)  # noqa: SLF001


def test_mongodb_source_dispatches_to_mongo_ports_lazily():
    """No live connection is touched — motor connects lazily on first operation."""
    registry, secrets, knowledge = resolve_source("mongodb://localhost/db")
    assert isinstance(registry, StoreRegistry)
    assert isinstance(secrets, StoreSecrets)
    assert isinstance(knowledge, StoreKnowledge)
    assert isinstance(registry._driver, MongoDriver)  # noqa: SLF001


def test_postgres_source_missing_extra_raises_actionable_error(monkeypatch):
    # llmbroker.postgres.driver is already cached (imported at this module's top),
    # so the guard around it would silently no-op unless it's evicted too.
    monkeypatch.delitem(sys.modules, "llmbroker.postgres.driver", raising=False)
    monkeypatch.setitem(sys.modules, "asyncpg", None)
    with pytest.raises(ImportError, match=r"pip install llmbroker\[postgres\]"):
        resolve_source("postgresql://localhost/db")


def test_mongodb_source_missing_extra_raises_actionable_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "motor", None)
    monkeypatch.setitem(sys.modules, "motor.motor_asyncio", None)
    with pytest.raises(ImportError, match=r"pip install llmbroker\[mongodb\]"):
        resolve_source("mongodb://localhost/db")


def test_sync_broker_sqlite_source_wires_three_ports(tmp_path):
    db_path = str(tmp_path / "broker.db")

    async def _seed():
        await llmbroker.sqlite.Registry(db_path).mirror([_cfg()])
        await llmbroker.sqlite.Secrets(db_path).set("KEY", "secret-value")

    asyncio.run(_seed())

    with Broker(db_path) as broker:
        assert broker.count() == 1

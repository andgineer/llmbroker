"""Tests for the source-parameter dispatch on AsyncBroker/Broker."""

import asyncio
import sys

import llmbroker
import pytest

from llmbroker.backends.ports import DriverStore, DriverRegistry, DriverSecrets
from llmbroker.broker.broker import AsyncBroker
from llmbroker.broker.source import resolve_source
from llmbroker.models import LLMConfig
from llmbroker.mongodb.driver import MongoDriver
from llmbroker.postgres.driver import PostgresDriver
from llmbroker.sqlite.driver import SqliteDriver
from llmbroker.sqlite import Registry as SqliteRegistry
from llmbroker.sqlite import Secrets as SqliteSecrets
from llmbroker.standalone.registry import Registry as FileRegistry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.sync import Broker


def _cfg(name: str = "llm1", api_key_ref: str = "KEY") -> LLMConfig:
    return LLMConfig(name=name, base_url="https://x/v1", model="m", api_key_ref=api_key_ref)


async def test_sqlite_source_wires_three_ports(tmp_path):
    db_path = str(tmp_path / "broker.db")
    registry, secrets, store = resolve_source(db_path)
    assert isinstance(registry, DriverRegistry)
    assert isinstance(secrets, DriverSecrets)
    assert isinstance(store, DriverStore)

    await registry.mirror([_cfg()])
    await secrets.set("KEY", "secret-value")

    async with AsyncBroker(db_path) as broker:
        assert await broker.count() == 1
        assert (
            await broker._shared_ring.resolve(broker._pool.config("llm1").api_key_ref)
            == "secret-value"
        )


def test_sqlite_source_dot_sqlite_suffix_and_url_form_both_dispatch(tmp_path):
    for path in (str(tmp_path / "a.sqlite"), f"sqlite://{tmp_path / 'b.db'}"):
        registry, _secrets, _store = resolve_source(path)
        assert isinstance(registry, DriverRegistry)
        assert isinstance(registry._driver, SqliteDriver)  # noqa: SLF001


def test_a_config_file_path_is_refused_and_names_the_forms_that_work(tmp_path):
    """A model list is not a path a host names: the refusal has to point at the three
    shapes that remain, or the host has nowhere to go."""
    f = tmp_path / "llms.toml"
    f.write_text('[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n')

    with pytest.raises(ValueError, match="unrecognized registry source") as exc:
        resolve_source(str(f))
    message = str(exc.value)
    assert "Broker()" in message
    assert "sqlite" in message and "postgresql" in message and "mongodb" in message
    assert "registry object" in message

    with pytest.raises(ValueError, match="unrecognized registry source"):
        AsyncBroker(str(f))


def test_the_file_registry_is_not_public_api():
    """It stays importable from its own module — it is the port the home model list runs
    on — but exporting it would keep alive the shape the path form was removed to close."""
    assert "Registry" not in llmbroker.__all__
    assert not hasattr(llmbroker, "Registry")


def test_unrecognized_source_raises_clear_error():
    with pytest.raises(ValueError, match="unrecognized registry source"):
        resolve_source("not-a-known-form")


def test_no_registry_is_the_curated_pool_in_the_home_directory(llmbroker_home):
    """No source is a source: the zero-config installation. See
    tests/test_fileless_broker.py for what it then does."""
    broker = AsyncBroker()
    assert isinstance(broker._registry, FileRegistry)
    assert broker._registry.path == llmbroker_home / "model-list.toml"


def test_sync_broker_no_registry_builds_the_same_installation(llmbroker_home):
    broker = Broker()
    try:
        assert broker._async._registry.path == llmbroker_home / "model-list.toml"
    finally:
        broker.close()


async def test_explicit_secrets_override_wins_over_sqlite_source(tmp_path):
    db_path = str(tmp_path / "broker.db")
    await SqliteRegistry(db_path).mirror([_cfg()])
    await SqliteSecrets(db_path).set("KEY", "from-sqlite")
    override_secrets = DictSecrets({"KEY": "from-override"})

    async with AsyncBroker(db_path, secrets=override_secrets) as broker:
        assert (
            await broker._shared_ring.resolve(broker._pool.config("llm1").api_key_ref)
            == "from-override"
        )


def test_postgres_source_dispatches_to_postgres_ports_lazily():
    """No live connection is touched — pool creation is deferred to first ``ensure_schema()``."""
    registry, secrets, store = resolve_source("postgresql://localhost/db")
    assert isinstance(registry, DriverRegistry)
    assert isinstance(secrets, DriverSecrets)
    assert isinstance(store, DriverStore)
    assert isinstance(registry._driver, PostgresDriver)  # noqa: SLF001


def test_mongodb_source_dispatches_to_mongo_ports_lazily():
    """No live connection is touched — motor connects lazily on first operation."""
    registry, secrets, store = resolve_source("mongodb://localhost/db")
    assert isinstance(registry, DriverRegistry)
    assert isinstance(secrets, DriverSecrets)
    assert isinstance(store, DriverStore)
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
        await SqliteRegistry(db_path).mirror([_cfg()])
        await SqliteSecrets(db_path).set("KEY", "secret-value")

    asyncio.run(_seed())

    with Broker(db_path) as broker:
        assert broker.count() == 1

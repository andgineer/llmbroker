"""Unit tests for Catalog: provision/resync reconciliation and sync() mirroring."""

import asyncio
import logging

import pytest

from llmbroker.broker import presets
from llmbroker.broker.broker import AsyncBroker
from llmbroker.broker.catalog import Catalog
from llmbroker.broker.pool import LLMPool
from llmbroker.exceptions import EmptyRegistryError
from llmbroker.models import LLMConfig
from llmbroker.sqlite import Store as SqliteStore
from llmbroker.sqlite import Registry as SqliteRegistry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import InMemoryStore


class _MutableRegistry:
    def __init__(self, initial=None):
        self._store: dict[str, LLMConfig] = {c.name: c for c in (initial or [])}

    async def load(self, user_id=None):
        return list(self._store.values())

    async def mirror(self, configs, user_id=None):
        names = {c.name for c in configs}
        for name in list(self._store):
            if name not in names:
                del self._store[name]
        for cfg in configs:
            self._store[cfg.name] = cfg


class _ReadOnlyRegistry:
    def __init__(self, configs):
        self._configs = list(configs)

    async def load(self, user_id=None):
        return list(self._configs)


class _NoSecrets:
    async def resolve(self, ref, user_id=None):
        raise KeyError(ref)


def _cfg(name, url="https://x/v1", model="m", *, from_preset=True):
    return LLMConfig(name=name, base_url=url, model=model, api_key_ref="K", from_preset=from_preset)


# ── apply(): writes the merged lineup, exactly as given ──────────────────────


def test_apply_adds_new_entries():
    async def run():
        registry = _MutableRegistry()
        pool = LLMPool()
        catalog = Catalog(registry, _NoSecrets(), pool, scope=None)
        await catalog.apply([_cfg("p1"), _cfg("p2")])
        assert {c.name for c in await registry.load()} == {"p1", "p2"}

    asyncio.run(run())


def test_apply_updates_existing_entries():
    async def run():
        registry = _MutableRegistry([_cfg("p1", "https://old/v1")])
        pool = LLMPool()
        catalog = Catalog(registry, _NoSecrets(), pool, scope=None)
        await catalog.apply([_cfg("p1", "https://new/v1")])
        loaded = {c.name: c for c in await registry.load()}
        assert loaded["p1"].base_url == "https://new/v1"

    asyncio.run(run())


def test_apply_deletes_entries_absent_from_the_merged_lineup():
    """apply() is the write half and mirrors what it is handed — deciding what may
    leave is the merge engine's job, not this one's."""

    async def run():
        registry = _MutableRegistry([_cfg("stale")])
        pool = LLMPool()
        catalog = Catalog(registry, _NoSecrets(), pool, scope=None)
        await catalog.apply([_cfg("p1")])
        names = {c.name for c in await registry.load()}
        assert names == {"p1"}

    asyncio.run(run())


def test_apply_requires_mutable_registry():
    async def run():
        pool = LLMPool()
        catalog = Catalog(_ReadOnlyRegistry([]), _NoSecrets(), pool, scope=None)
        try:
            await catalog.apply([_cfg("p1")])
        except TypeError as exc:
            assert "does not support mutations" in str(exc)
        else:
            raise AssertionError("expected TypeError")

    asyncio.run(run())


# ── provision(): reconciles the pool, fails fast on an empty registry ───────


def test_provision_populates_pool_from_registry():
    async def run():
        registry = _MutableRegistry([_cfg("p1"), _cfg("p2")])
        pool = LLMPool()
        catalog = Catalog(registry, DictSecrets({"K": "key"}), pool, scope=None)
        await catalog.provision()
        assert set(pool.configs) == {"p1", "p2"}

    asyncio.run(run())


def test_provision_raises_on_empty_registry():
    async def run():
        registry = _MutableRegistry()
        pool = LLMPool()
        catalog = Catalog(registry, _NoSecrets(), pool, scope=None)
        try:
            await catalog.provision()
        except EmptyRegistryError as exc:
            assert 'broker.sync("freetier")' in str(exc)
        else:
            raise AssertionError("expected EmptyRegistryError")

    asyncio.run(run())


def test_empty_registry_error_is_still_a_runtime_error():
    """Hosts written against the untyped raise keep working — the property that
    makes the typed exception an additive change."""

    async def run():
        catalog = Catalog(_MutableRegistry(), _NoSecrets(), LLMPool(), scope=None)
        with pytest.raises(RuntimeError):
            await catalog.provision()

    asyncio.run(run())


def test_resync_reconciles_pool_membership():
    async def run():
        registry = _MutableRegistry([_cfg("p1")])
        pool = LLMPool()
        catalog = Catalog(registry, DictSecrets({"K": "key"}), pool, scope=None)
        await catalog.provision()
        assert set(pool.configs) == {"p1"}

        await registry.mirror([_cfg("p1"), _cfg("p2")])
        await catalog.resync()
        assert set(pool.configs) == {"p1", "p2"}

    asyncio.run(run())


# ── Partial-key framing: an unresolved key is normal, not alarming ──────────


def test_unresolved_key_logs_info_not_warning(caplog):
    async def run():
        pool = LLMPool()
        catalog = Catalog(_ReadOnlyRegistry([_cfg("p1")]), _NoSecrets(), pool, scope=None)
        with caplog.at_level(logging.INFO, logger="llmbroker.broker"):
            await catalog.provision()

    asyncio.run(run())
    assert any(r.levelno == logging.INFO and "not resolved" in r.message for r in caplog.records)
    assert not any(r.levelno == logging.WARNING for r in caplog.records)


# ── Scope-prefixed key resolution (own key, falling back to the shared ref) ─


def test_resolve_key_prefers_scope_prefixed_ref():
    async def run():
        pool = LLMPool()
        secrets = DictSecrets({"K": "shared", "alice/K": "alice-own"})
        catalog = Catalog(_MutableRegistry([_cfg("p1")]), secrets, pool, scope="alice")
        await catalog.provision()
        assert pool.resolved_key("p1") == "alice-own"

    asyncio.run(run())


def test_resolve_key_falls_back_to_shared_ref_without_own_key():
    async def run():
        pool = LLMPool()
        secrets = DictSecrets({"K": "shared"})
        catalog = Catalog(_MutableRegistry([_cfg("p1")]), secrets, pool, scope="alice")
        await catalog.provision()
        assert pool.resolved_key("p1") == "shared"

    asyncio.run(run())


# ── An empty value is no key, whatever backend produced it ──────────────────


class _BlankSecrets:
    """A backend that answers every ref with a blank value (Vault/AWS/DB can)."""

    def __init__(self, value=""):
        self._value = value

    async def resolve(self, ref, user_id=None):
        return self._value


@pytest.mark.parametrize("blank", ["", "   \n"])
def test_blank_backend_value_leaves_the_slot_keyless(blank):
    async def run():
        pool = LLMPool()
        catalog = Catalog(_MutableRegistry([_cfg("p1")]), _BlankSecrets(blank), pool, scope=None)
        await catalog.provision()
        assert not pool.has_key("p1")

    asyncio.run(run())


def test_blank_scope_prefixed_value_falls_back_to_the_shared_ref():
    async def run():
        pool = LLMPool()
        secrets = DictSecrets({"K": "shared", "alice/K": "  "})
        catalog = Catalog(_MutableRegistry([_cfg("p1")]), secrets, pool, scope="alice")
        await catalog.provision()
        assert pool.resolved_key("p1") == "shared"

    asyncio.run(run())


class _RecordingSecrets:
    """Mutable backend recording every ``set`` — the seed's observable effect."""

    def __init__(self, initial=None):
        self._store = dict(initial or {})
        self.written: list[tuple[str, str]] = []

    async def resolve(self, ref, user_id=None):
        if ref not in self._store:
            raise KeyError(ref)
        return self._store[ref]

    async def set(self, ref, value, user_id=None):
        self.written.append((ref, value))
        self._store[ref] = value


async def test_seed_does_not_copy_a_blank_bootstrap_value(monkeypatch):
    monkeypatch.setenv("K", "  ")
    secrets = _RecordingSecrets()
    catalog = Catalog(_MutableRegistry(), secrets, LLMPool(), scope=None)
    await catalog.apply([_cfg("p1")])
    assert secrets.written == []


async def test_seed_replaces_a_blank_existing_value(monkeypatch):
    """A blank stored value is not "already resolvable — preserve": it is absent,
    so the env-resolvable key seeds over it."""
    monkeypatch.setenv("K", "from-env")
    secrets = _RecordingSecrets({"K": ""})
    catalog = Catalog(_MutableRegistry(), secrets, LLMPool(), scope=None)
    await catalog.apply([_cfg("p1")])
    assert secrets.written == [("K", "from-env")]


# ── Broker-level: AsyncBroker.sync() end to end ─────────────────────────────


@pytest.fixture
def served(monkeypatch):
    """Serve a one-entry curated lineup to ``sync("freetier")``."""
    monkeypatch.setattr(
        presets,
        "fetch_preset_text",
        lambda _name: '[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n',
    )


async def test_broker_sync_mirrors_preset_into_sqlite_registry(tmp_path, served):
    db = str(tmp_path / "b.db")
    broker = AsyncBroker(
        registry=SqliteRegistry(db),
        secrets=DictSecrets({"K": "key"}),
        store=InMemoryStore(),
        sync=None,
    )
    await broker.sync("freetier")
    await broker.aclose()

    reg = SqliteRegistry(db)
    assert {c.name for c in await reg.load()} == {"p1"}


async def test_broker_provision_without_sync_raises(tmp_path):
    db = str(tmp_path / "b.db")
    broker = AsyncBroker(
        registry=SqliteRegistry(db),
        store=InMemoryStore(),
        sync=None,
    )
    try:
        await broker.count()
    except EmptyRegistryError as exc:
        assert 'broker.sync("freetier")' in str(exc)
    else:
        raise AssertionError("expected EmptyRegistryError")


async def test_broker_sync_then_provision_succeeds(tmp_path, served):
    db = str(tmp_path / "b.db")
    broker = AsyncBroker(
        registry=SqliteRegistry(db),
        secrets=DictSecrets({"K": "key"}),
        store=InMemoryStore(),
        sync=None,
    )
    await broker.sync("freetier")
    async with broker:
        assert await broker.count() == 1
        assert (await broker.get("p1")).config.name == "p1"


async def test_broker_sync_seeds_disabled_map(tmp_path, served):
    db = str(tmp_path / "b.db")
    broker = AsyncBroker(
        registry=SqliteRegistry(db),
        secrets=DictSecrets({"K": "key"}),
        store=SqliteStore(db),
        sync=None,
    )
    await broker.sync("freetier")
    await broker.aclose()

    assert await SqliteStore(db).get_disabled("p1") is False


async def test_manual_latch_survives_sync_reseed(tmp_path, served):
    """The disabled verdict lives in the store disabled-map, not the registry —
    a sync that re-mirrors the preset never touches it."""
    db = str(tmp_path / "b.db")
    reg = SqliteRegistry(db)
    await reg.mirror([_cfg("p1")])
    await SqliteStore(db).set_disabled("p1", True)

    broker = AsyncBroker(
        registry=SqliteRegistry(db),
        secrets=DictSecrets({"K": "key"}),
        store=SqliteStore(db),
        sync=None,
    )
    await broker.sync("freetier")
    async with broker:
        assert broker._pool.is_disabled("p1")
        assert await SqliteStore(db).get_disabled("p1") is True

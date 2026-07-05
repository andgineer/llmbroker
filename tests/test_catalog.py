"""Unit tests for apply_seed: seed policy branching logic in isolation."""

import asyncio
import inspect
import logging

import llmbroker.sqlite

from llmbroker.broker import AsyncBroker
from llmbroker.broker.catalog import Catalog, apply_seed
from llmbroker.broker.pool import LLMPool
from llmbroker.models import LLMConfig, LLMProfile, Origin, SeedPolicy
from llmbroker.standalone.registry import Registry as TomlRegistry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.telemetry import NoTelemetry


class _MutableRegistry:
    def __init__(self, initial=None):
        self._store: dict[str, LLMConfig] = {c.name: c for c in (initial or [])}

    async def load(self, user_id=None):
        return list(self._store.values())

    async def get(self, name, user_id=None):
        return self._store.get(name)

    async def add(self, cfg, user_id=None):
        self._store[cfg.name] = cfg

    async def update(self, cfg, user_id=None):
        self._store[cfg.name] = cfg

    async def remove(self, name, user_id=None):
        self._store.pop(name, None)

    async def read_profiles(self, user_id=None):
        return {}

    async def write_profile(self, name, profile, user_id=None):
        pass


class _ReadOnlyRegistry:
    def __init__(self, configs):
        self._configs = list(configs)

    async def load(self, user_id=None):
        return list(self._configs)


class _NoSecrets:
    async def resolve(self, ref, user_id=None):
        raise KeyError(ref)


def _cfg(name, url="https://x/v1"):
    return LLMConfig(name=name, base_url=url, model="m", api_key_ref="K")


def test_if_empty_seeds_when_registry_is_empty():
    async def run():
        registry = _MutableRegistry()
        source = _ReadOnlyRegistry([_cfg("p1"), _cfg("p2")])
        await apply_seed(registry, _NoSecrets(), source, SeedPolicy.IF_EMPTY, None)
        assert {c.name for c in await registry.load()} == {"p1", "p2"}

    asyncio.run(run())


def test_if_empty_skips_when_registry_has_entries():
    async def run():
        registry = _MutableRegistry([_cfg("existing")])
        source = _ReadOnlyRegistry([_cfg("new")])
        await apply_seed(registry, _NoSecrets(), source, SeedPolicy.IF_EMPTY, None)
        assert {c.name for c in await registry.load()} == {"existing"}

    asyncio.run(run())


def test_add_adds_new_preserves_existing():
    async def run():
        registry = _MutableRegistry([_cfg("existing")])
        source = _ReadOnlyRegistry([_cfg("new")])
        await apply_seed(registry, _NoSecrets(), source, SeedPolicy.ADD, None)
        assert {c.name for c in await registry.load()} == {"existing", "new"}

    asyncio.run(run())


def test_add_does_not_overwrite_existing():
    async def run():
        registry = _MutableRegistry([_cfg("p1", "https://original/v1")])
        source = _ReadOnlyRegistry([_cfg("p1", "https://replacement/v1")])
        await apply_seed(registry, _NoSecrets(), source, SeedPolicy.ADD, None)
        loaded = {c.name: c for c in await registry.load()}
        assert loaded["p1"].base_url == "https://original/v1"

    asyncio.run(run())


def test_mirror_adds_new():
    async def run():
        registry = _MutableRegistry()
        source = _ReadOnlyRegistry([_cfg("p1")])
        await apply_seed(registry, _NoSecrets(), source, SeedPolicy.MIRROR, None)
        assert {c.name for c in await registry.load()} == {"p1"}

    asyncio.run(run())


def test_mirror_removes_stale():
    async def run():
        registry = _MutableRegistry([_cfg("stale")])
        source = _ReadOnlyRegistry([_cfg("p1")])
        await apply_seed(registry, _NoSecrets(), source, SeedPolicy.MIRROR, None)
        names = {c.name for c in await registry.load()}
        assert "stale" not in names

    asyncio.run(run())


def test_mirror_updates_existing():
    async def run():
        registry = _MutableRegistry([_cfg("p1", "https://old/v1")])
        source = _ReadOnlyRegistry([_cfg("p1", "https://new/v1")])
        await apply_seed(registry, _NoSecrets(), source, SeedPolicy.MIRROR, None)
        loaded = {c.name: c for c in await registry.load()}
        assert loaded["p1"].base_url == "https://new/v1"

    asyncio.run(run())


# ── Partial-key framing: an unresolved key is normal, not alarming ──────────


def test_unresolved_key_logs_info_not_warning(caplog):
    async def run():
        pool = LLMPool(state_store=None, user_id=None)
        catalog = Catalog(
            _ReadOnlyRegistry([_cfg("p1")]),
            _NoSecrets(),
            pool,
            seed=None,
            seed_policy=SeedPolicy.IF_EMPTY,
            user_id=None,
        )
        with caplog.at_level(logging.INFO, logger="llmbroker.broker"):
            await catalog.provision()

    asyncio.run(run())
    assert any(r.levelno == logging.INFO and "not resolved" in r.message for r in caplog.records)
    assert not any(r.levelno == logging.WARNING for r in caplog.records)


# ── SeedPolicy.SYNC ───────────────────────────────────────────────────────────


def test_sync_adds_preset_new_model():
    async def run():
        registry = _MutableRegistry()
        source = _ReadOnlyRegistry([_cfg("p1")])
        alerts = await apply_seed(registry, _NoSecrets(), source, SeedPolicy.SYNC, None)
        loaded = {c.name: c for c in await registry.load()}
        assert loaded["p1"].origin is Origin.PRESET
        assert loaded["p1"].deprecated is False
        assert alerts == []

    asyncio.run(run())


def test_sync_updates_changed_parallel():
    async def run():
        old = LLMConfig(
            name="p1",
            base_url="https://x/v1",
            model="m",
            api_key_ref="K",
            origin=Origin.PRESET,
        )
        registry = _MutableRegistry([old])
        new_cfg = LLMConfig(
            name="p1",
            base_url="https://x/v1",
            model="m",
            api_key_ref="K",
            parallel=3,
        )
        source = _ReadOnlyRegistry([new_cfg])
        await apply_seed(registry, _NoSecrets(), source, SeedPolicy.SYNC, None)
        loaded = {c.name: c for c in await registry.load()}
        assert loaded["p1"].parallel == 3

    asyncio.run(run())


def test_sync_refuses_changed_model_under_existing_name():
    async def run():
        old = LLMConfig(
            name="p1",
            base_url="https://x/v1",
            model="model-a",
            api_key_ref="K",
            origin=Origin.PRESET,
        )
        registry = _MutableRegistry([old])
        source = _ReadOnlyRegistry(
            [LLMConfig(name="p1", base_url="https://x/v1", model="model-b", api_key_ref="K")],
        )
        alerts = await apply_seed(registry, _NoSecrets(), source, SeedPolicy.SYNC, None)
        loaded = {c.name: c for c in await registry.load()}
        assert loaded["p1"].model == "model-a"  # entry intact — untouched
        assert len(alerts) == 1
        assert "model-a" in alerts[0]
        assert "model-b" in alerts[0]

    asyncio.run(run())


def test_sync_deprecates_dropped_preset_origin_model():
    async def run():
        dropped = LLMConfig(
            name="old",
            base_url="https://x/v1",
            model="m",
            api_key_ref="K",
            origin=Origin.PRESET,
        )
        registry = _MutableRegistry([dropped])
        source = _ReadOnlyRegistry([])  # "old" no longer in the preset
        await apply_seed(registry, _NoSecrets(), source, SeedPolicy.SYNC, None)
        loaded = {c.name: c for c in await registry.load()}
        assert "old" in loaded  # never deleted
        assert loaded["old"].deprecated is True

    asyncio.run(run())


def test_sync_lifts_deprecation_on_reappearance():
    async def run():
        was_deprecated = LLMConfig(
            name="p1",
            base_url="https://x/v1",
            model="m",
            api_key_ref="K",
            origin=Origin.PRESET,
            deprecated=True,
        )
        registry = _MutableRegistry([was_deprecated])
        source = _ReadOnlyRegistry(
            [LLMConfig(name="p1", base_url="https://x/v1", model="m", api_key_ref="K")],
        )
        await apply_seed(registry, _NoSecrets(), source, SeedPolicy.SYNC, None)
        loaded = {c.name: c for c in await registry.load()}
        assert loaded["p1"].deprecated is False

    asyncio.run(run())


def test_sync_leaves_user_added_model_alone():
    async def run():
        user_added = LLMConfig(
            name="p1",
            base_url="https://mine/v1",
            model="mine",
            api_key_ref="MY_KEY",
            origin=Origin.USER,
        )
        registry = _MutableRegistry([user_added])
        source = _ReadOnlyRegistry(
            [LLMConfig(name="p1", base_url="https://preset/v1", model="preset-m", api_key_ref="K")],
        )
        await apply_seed(registry, _NoSecrets(), source, SeedPolicy.SYNC, None)
        loaded = {c.name: c for c in await registry.load()}
        assert loaded["p1"].base_url == "https://mine/v1"
        assert loaded["p1"].model == "mine"

    asyncio.run(run())


def test_sync_leaves_legacy_no_origin_model_alone():
    """Regression: an entry written before ``origin`` existed (or by external tooling)
    loads with ``origin=None``, which is not ``Origin.USER`` — SYNC used to treat it as
    preset-owned and silently overwrite it. Only ``origin=Origin.PRESET`` may be synced."""

    async def run():
        legacy = LLMConfig(
            name="p1",
            base_url="https://mine/v1",
            model="m",
            api_key_ref="MY_KEY",
        )
        assert legacy.origin is None
        registry = _MutableRegistry([legacy])
        source = _ReadOnlyRegistry(
            [LLMConfig(name="p1", base_url="https://preset/v1", model="m", api_key_ref="K")],
        )
        await apply_seed(registry, _NoSecrets(), source, SeedPolicy.SYNC, None)
        loaded = {c.name: c for c in await registry.load()}
        assert loaded["p1"].base_url == "https://mine/v1"
        assert loaded["p1"].api_key_ref == "MY_KEY"

    asyncio.run(run())


def test_sync_never_deletes_absent_user_origin_entry():
    async def run():
        user_added = LLMConfig(
            name="mine",
            base_url="https://mine/v1",
            model="m",
            api_key_ref="K",
            origin=Origin.USER,
        )
        registry = _MutableRegistry([user_added])
        source = _ReadOnlyRegistry([])
        await apply_seed(registry, _NoSecrets(), source, SeedPolicy.SYNC, None)
        assert "mine" in {c.name for c in await registry.load()}

    asyncio.run(run())


def test_catalog_update_stamps_origin_user():
    """Regression: update() used to leave a fresh cfg's origin=None, silently
    dropping the SYNC-protection add() grants."""

    async def run():
        registry = _MutableRegistry()
        pool = LLMPool(state_store=None, user_id=None)
        catalog = Catalog(
            registry,
            _NoSecrets(),
            pool,
            seed=None,
            seed_policy=SeedPolicy.SYNC,
            user_id=None,
        )
        await catalog.add(_cfg("p1"))
        await catalog.update(_cfg("p1", "https://new/v1"))
        loaded = {c.name: c for c in await registry.load()}
        assert loaded["p1"].origin is Origin.USER
        assert loaded["p1"].base_url == "https://new/v1"

    asyncio.run(run())


def test_sync_leaves_updated_user_model_alone():
    """Regression: without stamping origin on update(), a subsequent SYNC reseed
    would silently overwrite the user's edit back to the preset's values."""

    async def run():
        registry = _MutableRegistry()
        pool = LLMPool(state_store=None, user_id=None)
        catalog = Catalog(
            registry,
            _NoSecrets(),
            pool,
            seed=None,
            seed_policy=SeedPolicy.SYNC,
            user_id=None,
        )
        await catalog.add(_cfg("p1"))
        await catalog.update(_cfg("p1", "https://mine/v1"))

        source = _ReadOnlyRegistry(
            [LLMConfig(name="p1", base_url="https://preset/v1", model="m", api_key_ref="K")],
        )
        await apply_seed(registry, _NoSecrets(), source, SeedPolicy.SYNC, None)

        loaded = {c.name: c for c in await registry.load()}
        assert loaded["p1"].base_url == "https://mine/v1"

    asyncio.run(run())


def test_broker_default_seed_policy_is_sync():
    assert (
        inspect.signature(AsyncBroker.__init__).parameters["seed_policy"].default is SeedPolicy.SYNC
    )


async def test_sync_deprecated_entry_stays_routable(tmp_path):
    """Broker-level: a SYNC-deprecated model keeps its slot and profile — the
    deprecated marker is a registry-only concern, it has no effect on selection."""
    db = str(tmp_path / "b.db")
    reg = llmbroker.sqlite.Registry(db)
    await reg.add(
        LLMConfig(
            name="dep",
            base_url="https://x/v1",
            model="m",
            api_key_ref="K",
            origin=Origin.PRESET,
        ),
    )
    seed_path = tmp_path / "preset.toml"
    seed_path.write_text("")  # "dep" is absent from the preset -> gets deprecated

    async with AsyncBroker(
        registry=llmbroker.sqlite.Registry(db),
        secrets=DictSecrets({"K": "key"}),
        telemetry=NoTelemetry(),
        seed=TomlRegistry(seed_path),
        seed_policy=SeedPolicy.SYNC,
    ) as broker:
        picked = await broker._pool.acquire(0, operation=None)
        assert picked.name == "dep"  # still routable


async def test_manual_latch_survives_sync_reseed(tmp_path):
    db = str(tmp_path / "b.db")
    reg = llmbroker.sqlite.Registry(db)
    await reg.add(
        LLMConfig(
            name="p1", base_url="https://x/v1", model="m", api_key_ref="K", origin=Origin.PRESET
        ),
    )
    await reg.write_profile("p1", LLMProfile(benched=True, benched_reason="manual review"))

    seed_path = tmp_path / "preset.toml"
    seed_path.write_text(
        '[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n',
    )

    async with AsyncBroker(
        registry=llmbroker.sqlite.Registry(db),
        secrets=DictSecrets({"K": "key"}),
        telemetry=NoTelemetry(),
        seed=TomlRegistry(seed_path),
        seed_policy=SeedPolicy.SYNC,
    ) as broker:
        assert broker._pool.is_disabled("p1")
        profiles = await reg.read_profiles()
        assert profiles["p1"].benched is True
        assert profiles["p1"].benched_reason == "manual review"

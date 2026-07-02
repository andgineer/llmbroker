"""Unit tests for apply_seed: seed policy branching logic in isolation."""

import asyncio
import logging

from llmbroker.broker.catalog import Catalog, apply_seed
from llmbroker.broker.pool import LLMPool
from llmbroker.models import LLMConfig, SeedPolicy


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

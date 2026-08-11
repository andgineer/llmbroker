"""The pool is rebuilt on four triggers and at no other time: at start, on the
refresh clock, on an explicit ``sync()``, and on pool exhaustion — that last one
debounced, so a pool that stays exhausted under traffic does not storm the ports."""

import asyncio
import logging
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from llmbroker.broker import presets
from llmbroker.broker.broker import AsyncBroker
from llmbroker.exceptions import NoLLMAvailableError
from llmbroker.models import LLMConfig
from llmbroker.sqlite import Registry as SqliteRegistry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import InMemoryStore

_PATCH = "llmbroker.broker.router.call_provider"
_PRESET = '[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n'


@pytest.fixture(autouse=True)
def preset(monkeypatch):
    monkeypatch.setattr(presets, "fetch_preset_text", lambda _name: _PRESET)


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {}
    resp.text = f"HTTP {status}"
    return httpx.HTTPStatusError("err", request=MagicMock(), response=resp)


class _CountingRegistry:
    """A registry that says how many times the pool has gone back to it."""

    def __init__(self, configs):
        self._configs = list(configs)
        self.loads = 0

    async def load(self):
        self.loads += 1
        return list(self._configs)


class _ListingSecrets:
    """An enumerable backend whose listing is a round trip — AWS, Vault, a DB."""

    def __init__(self, mapping):
        self._mapping = dict(mapping)
        self.listings = 0

    async def resolve(self, ref):
        await asyncio.sleep(0)
        if ref not in self._mapping:
            raise KeyError(ref)
        return self._mapping[ref]

    async def refs(self, prefix=""):
        self.listings += 1
        await asyncio.sleep(0)
        return frozenset(r for r in self._mapping if r.startswith(prefix))


def _cfg(name="p1", ref="K"):
    return LLMConfig(name=name, base_url="https://x/v1", model="m", api_key_ref=ref)


def _broker(registry, **kwargs):
    kwargs.setdefault("secrets", DictSecrets({"K": "key"}))
    kwargs.setdefault("store", InMemoryStore())
    kwargs.setdefault("sync", None)
    return AsyncBroker(registry, **kwargs)


# ── Nothing else rebuilds ────────────────────────────────────────────────────


async def test_an_idle_broker_reads_nothing_across_more_than_a_clock_period(monkeypatch):
    """Automatic fetching is off, so the refresh clock cannot answer for this one:
    what is being asserted is that idling alone rebuilds nothing."""
    registry = _CountingRegistry([_cfg()])
    async with _broker(registry, sync_interval=None) as broker:
        assert await broker.count() == 1
        at_start = registry.loads
        for _ in range(50):
            await asyncio.sleep(0)
        assert registry.loads == at_start


async def test_successful_calls_produce_no_rebuild_at_all(monkeypatch):
    monkeypatch.setattr(_PATCH, AsyncMock(return_value=("ok", None, None)))
    registry = _CountingRegistry([_cfg()])
    async with _broker(registry, sync_interval=None) as broker:
        await broker.ensure_pool()
        at_start = registry.loads
        for _ in range(20):
            assert (await broker.ask("hi")).text == "ok"
        assert registry.loads == at_start


# ── Trigger 1: provisioning ──────────────────────────────────────────────────


async def test_provisioning_rebuilds_once(monkeypatch):
    registry = _CountingRegistry([_cfg()])
    async with _broker(registry, sync_interval=None) as broker:
        await broker.ensure_pool()
        after_first = registry.loads
        await broker.ensure_pool()
        assert registry.loads == after_first
    assert after_first >= 1


# ── Trigger 2: pool exhaustion, debounced ────────────────────────────────────


async def test_exhaustion_triggers_a_rebuild_and_the_debounce_stops_the_second(monkeypatch):
    monkeypatch.setattr(_PATCH, AsyncMock(side_effect=_http_status_error(429)))
    registry = _CountingRegistry([_cfg()])
    async with _broker(registry, sync_interval=None) as broker:
        await broker.ensure_pool()
        at_start = registry.loads

        with pytest.raises(NoLLMAvailableError):
            await broker.ask("hi", wait=0)
        after_first = registry.loads
        assert after_first > at_start

        with pytest.raises(NoLLMAvailableError):
            await broker.ask("hi", wait=0)
        assert registry.loads == after_first


async def test_the_debounce_lets_the_next_period_through(monkeypatch):
    monkeypatch.setattr(_PATCH, AsyncMock(side_effect=_http_status_error(429)))
    registry = _CountingRegistry([_cfg()])
    async with _broker(registry, sync_interval=None) as broker:
        await broker.ensure_pool()
        with pytest.raises(NoLLMAvailableError):
            await broker.ask("hi", wait=0)
        after_first = registry.loads

        broker._next_exhaustion_rebuild = float("-inf")  # the period has elapsed
        with pytest.raises(NoLLMAvailableError):
            await broker.ask("hi", wait=0)
        assert registry.loads > after_first


async def test_a_rebuild_that_raises_never_reaches_the_caller(tmp_path, monkeypatch, caplog):
    """The reactive trigger runs off a call that has already failed; an unreadable
    port must leave the pool as it is and say so, not replace one error with another."""
    monkeypatch.setattr(_PATCH, AsyncMock(side_effect=_http_status_error(429)))

    class _BrokenRegistry(_CountingRegistry):
        async def load(self):
            await super().load()
            if self.loads > 1:
                raise OSError("registry is gone")
            return [_cfg()]

    async with _broker(_BrokenRegistry([_cfg()]), sync_interval=None) as broker:
        await broker.ensure_pool()
        with caplog.at_level(logging.ERROR, logger="llmbroker.broker"):
            with pytest.raises(NoLLMAvailableError):
                await broker.ask("hi", wait=0)
        assert await broker.count() == 1
    assert any("pool rebuild on pool exhaustion failed" in r.message for r in caplog.records)


# ── What a rebuild costs, and what it must survive ───────────────────────────


async def test_one_enumeration_of_the_secrets_store_per_rebuild():
    """However many callers a process has seen, a rebuild asks the store which refs
    it holds exactly once — a listing per caller is a metered API call per caller."""
    secrets = _ListingSecrets({"K": "key"})
    async with _broker(_CountingRegistry([_cfg()]), secrets=secrets, sync_interval=None) as broker:
        for i in range(20):
            broker.for_scope(f"user-{i}")
        secrets.listings = 0

        await broker.rebuild()

        assert secrets.listings == 1


async def test_a_caller_arriving_mid_rebuild_does_not_break_it():
    """A caller is built per request, so a request landing inside a rebuild is the
    normal case rather than the rare one."""
    secrets = _ListingSecrets({"K": "key", "alice/K": "a"})
    async with _broker(_CountingRegistry([_cfg()]), secrets=secrets, sync_interval=None) as broker:
        broker.for_scope("alice")
        broker.for_scope("bob")

        async def arrive():
            await asyncio.sleep(0)
            broker.for_scope("carol")

        await asyncio.gather(broker.rebuild(), arrive())
        assert (await broker.for_scope("carol").count()) == 1


async def test_the_refresh_catches_what_its_own_rebuild_raises(monkeypatch, caplog):
    """The refresh runs as a detached task and therefore catches everything: an
    escaping exception is lost rather than naming the port that failed."""

    class _BrokenRegistry(_CountingRegistry):
        async def load(self):
            await super().load()
            if self.loads > 2:
                raise OSError("registry is gone")
            return [_cfg()]

    async with _broker(
        _BrokenRegistry([_cfg()]),
        sync="freetier",
        sync_interval=0.0,  # the clock is always due
    ) as broker:
        with caplog.at_level(logging.ERROR, logger="llmbroker.broker"):
            await broker.ensure_pool()  # arms and fires the refresh task
            task = broker._refresher._task
            assert task is not None
            await task

        assert task.exception() is None
        assert any("pool rebuild on the refresh check failed" in r.message for r in caplog.records)
        assert await broker.count() == 1


# ── Trigger 3: an explicit sync ──────────────────────────────────────────────


async def test_an_explicit_sync_always_rebuilds(tmp_path, monkeypatch):
    """Applied or not: a sync bootstraps keys from the environment on every run, and
    a key that has just appeared is the reason a host calls one."""
    monkeypatch.setattr(_PATCH, AsyncMock(return_value=("ok", None, None)))
    db = str(tmp_path / "b.db")
    await SqliteRegistry(db).mirror([replace(_cfg(), from_preset=True)])
    async with _broker(SqliteRegistry(db), sync_interval=None) as broker:
        await broker.ensure_pool()
        rebuilds = 0
        real = broker.rebuild

        async def counting():
            nonlocal rebuilds
            rebuilds += 1
            await real()

        with patch.object(broker, "rebuild", new=counting):
            broker._refresher._rebuild = counting
            await broker.sync("freetier")
            await broker.sync("freetier")  # a no-op sync rebuilds too
        assert rebuilds == 2


async def test_a_key_stored_while_the_pool_is_exhausted_is_picked_up_by_the_next_call(
    monkeypatch,
):
    """What the reactive trigger exists for, end to end: no key at start, one stored
    by an admin, and the very next call uses it without waiting out the clock."""
    monkeypatch.setattr(_PATCH, AsyncMock(return_value=("ok", None, None)))
    secrets = DictSecrets({})
    async with _broker(_CountingRegistry([_cfg()]), secrets=secrets, sync_interval=None) as broker:
        with pytest.raises(NoLLMAvailableError):
            await broker.ask("hi", wait=0)

        secrets._mapping["K"] = "stored-now"  # noqa: SLF001 - test double, direct mutation
        broker._next_exhaustion_rebuild = float("-inf")
        with pytest.raises(NoLLMAvailableError):
            await broker.ask("hi", wait=0)  # this call is what re-reads the keys

        assert (await broker.ask("hi")).text == "ok"

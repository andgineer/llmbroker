"""Tests for the Redis state store."""

from datetime import UTC, datetime, timedelta

import fakeredis.aioredis

import llmbroker.redis
from llmbroker.models import LifecyclePhase, LLMState


def _store() -> llmbroker.redis.StateStore:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return llmbroker.redis.StateStore(client)


async def test_redis_state_store_read_empty():
    store = _store()
    assert await store.read() == {}


async def test_redis_state_store_write_and_read_available():
    store = _store()
    state = LLMState(phase=LifecyclePhase.AVAILABLE, fail_count=3)
    await store.write("p1", state)
    result = await store.read()
    assert "p1" in result
    assert result["p1"].phase is LifecyclePhase.AVAILABLE
    assert result["p1"].fail_count == 3
    assert result["p1"].cooldown_until is None


async def test_redis_state_store_write_and_read_cooling():
    store = _store()
    future = datetime.now(UTC) + timedelta(seconds=120)
    state = LLMState(phase=LifecyclePhase.COOLING, cooldown_until=future, fail_count=2)
    await store.write("p1", state)
    result = await store.read()
    assert result["p1"].phase is LifecyclePhase.COOLING
    assert result["p1"].cooldown_until is not None
    assert result["p1"].fail_count == 2


async def test_redis_state_store_expired_cooling_reads_as_available():
    store = _store()
    past = datetime.now(UTC) - timedelta(seconds=1)
    state = LLMState(phase=LifecyclePhase.COOLING, cooldown_until=past, fail_count=1)
    await store.write("p1", state)
    result = await store.read()
    assert result["p1"].phase is LifecyclePhase.AVAILABLE
    assert result["p1"].cooldown_until is None
    assert result["p1"].fail_count == 1


async def test_redis_state_store_overwrite():
    store = _store()
    future = datetime.now(UTC) + timedelta(seconds=60)
    await store.write(
        "p1", LLMState(phase=LifecyclePhase.COOLING, cooldown_until=future, fail_count=1)
    )
    await store.write("p1", LLMState(phase=LifecyclePhase.AVAILABLE, fail_count=0))
    result = await store.read()
    assert result["p1"].phase is LifecyclePhase.AVAILABLE


async def test_redis_state_store_per_user_isolated():
    store = _store()
    future = datetime.now(UTC) + timedelta(seconds=60)
    await store.write(
        "p1",
        LLMState(phase=LifecyclePhase.COOLING, cooldown_until=future, fail_count=1),
        "alice",
    )
    alice = await store.read("alice")
    bob = await store.read("bob")
    unscoped = await store.read()
    assert "p1" in alice
    assert "p1" not in bob
    assert "p1" not in unscoped


async def test_redis_state_store_user_id_none_unscoped():
    store = _store()
    await store.write("shared", LLMState(fail_count=5))
    await store.write("alice-p1", LLMState(fail_count=1), "alice")
    result = await store.read()
    assert list(result) == ["shared"]
    assert result["shared"].fail_count == 5


async def test_redis_state_store_offline_phase():
    store = _store()
    state = LLMState(phase=LifecyclePhase.OFFLINE, fail_count=0)
    await store.write("p1", state)
    result = await store.read()
    assert result["p1"].phase is LifecyclePhase.OFFLINE
    assert result["p1"].cooldown_until is None

"""Integration tests for mongodb.StateStore."""

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("motor.motor_asyncio")

from llmbroker.models import LifecyclePhase, LLMState
from llmbroker.mongodb import StateStore


@pytest.fixture(autouse=True)
async def clean(mongo_db):
    yield
    await mongo_db["llmbroker_state"].delete_many({})


async def test_read_empty(mongo_db):
    store = StateStore(mongo_db)
    assert await store.read() == {}


async def test_write_and_read_available(mongo_db):
    store = StateStore(mongo_db)
    state = LLMState(phase=LifecyclePhase.AVAILABLE, fail_count=3)
    await store.write("p1", state)
    result = await store.read()
    assert result["p1"].phase is LifecyclePhase.AVAILABLE
    assert result["p1"].fail_count == 3
    assert result["p1"].cooldown_until is None


async def test_write_and_read_cooling(mongo_db):
    store = StateStore(mongo_db)
    future = datetime.now(UTC) + timedelta(seconds=120)
    state = LLMState(phase=LifecyclePhase.COOLING, cooldown_until=future, fail_count=2)
    await store.write("p1", state)
    result = await store.read()
    assert result["p1"].phase is LifecyclePhase.COOLING
    assert result["p1"].cooldown_until is not None
    assert result["p1"].fail_count == 2


async def test_expired_cooling_reads_as_available(mongo_db):
    store = StateStore(mongo_db)
    past = datetime.now(UTC) - timedelta(seconds=1)
    state = LLMState(phase=LifecyclePhase.COOLING, cooldown_until=past, fail_count=1)
    await store.write("p1", state)
    result = await store.read()
    assert result["p1"].phase is LifecyclePhase.AVAILABLE
    assert result["p1"].cooldown_until is None
    assert result["p1"].fail_count == 1


async def test_overwrite_replaces_entry(mongo_db):
    store = StateStore(mongo_db)
    future = datetime.now(UTC) + timedelta(seconds=60)
    await store.write(
        "p1", LLMState(phase=LifecyclePhase.COOLING, cooldown_until=future, fail_count=1)
    )
    await store.write("p1", LLMState(phase=LifecyclePhase.AVAILABLE, fail_count=0))
    result = await store.read()
    assert result["p1"].phase is LifecyclePhase.AVAILABLE


async def test_per_user_isolated(mongo_db):
    store = StateStore(mongo_db)
    future = datetime.now(UTC) + timedelta(seconds=60)
    await store.write(
        "p1", LLMState(phase=LifecyclePhase.COOLING, cooldown_until=future, fail_count=1), "alice"
    )
    alice = await store.read("alice")
    bob = await store.read("bob")
    unscoped = await store.read()
    assert "p1" in alice
    assert "p1" not in bob
    assert "p1" not in unscoped


async def test_user_id_none_unscoped(mongo_db):
    store = StateStore(mongo_db)
    await store.write("shared", LLMState(fail_count=5))
    await store.write("alice-p1", LLMState(fail_count=1), "alice")
    result = await store.read()
    assert list(result) == ["shared"]
    assert result["shared"].fail_count == 5


async def test_aclose_is_noop(mongo_db):
    store = StateStore(mongo_db)
    await store.aclose()

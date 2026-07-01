"""Backend-parametrized tests for the StateStore contract (sqlite, postgres, mongodb, redis)."""

from datetime import UTC, datetime, timedelta

import llmbroker.mongodb

from llmbroker.models import LifecyclePhase, LLMState


async def test_read_empty(any_state_store):
    assert await any_state_store.read() == {}


async def test_write_and_read_available(any_state_store):
    state = LLMState(phase=LifecyclePhase.AVAILABLE, fail_count=3)
    await any_state_store.write("p1", state)
    result = await any_state_store.read()
    assert result["p1"].phase is LifecyclePhase.AVAILABLE
    assert result["p1"].fail_count == 3
    assert result["p1"].cooldown_until is None


async def test_write_and_read_cooling(any_state_store):
    future = datetime.now(UTC) + timedelta(seconds=120)
    state = LLMState(phase=LifecyclePhase.COOLING, cooldown_until=future, fail_count=2)
    await any_state_store.write("p1", state)
    result = await any_state_store.read()
    assert result["p1"].phase is LifecyclePhase.COOLING
    assert result["p1"].cooldown_until is not None
    assert result["p1"].fail_count == 2


async def test_expired_cooling_reads_as_available(any_state_store):
    past = datetime.now(UTC) - timedelta(seconds=1)
    state = LLMState(phase=LifecyclePhase.COOLING, cooldown_until=past, fail_count=1)
    await any_state_store.write("p1", state)
    result = await any_state_store.read()
    assert result["p1"].phase is LifecyclePhase.AVAILABLE
    assert result["p1"].cooldown_until is None
    assert result["p1"].fail_count == 1


async def test_overwrite_replaces_entry(any_state_store):
    future = datetime.now(UTC) + timedelta(seconds=60)
    await any_state_store.write(
        "p1", LLMState(phase=LifecyclePhase.COOLING, cooldown_until=future, fail_count=1)
    )
    await any_state_store.write("p1", LLMState(phase=LifecyclePhase.AVAILABLE, fail_count=0))
    result = await any_state_store.read()
    assert result["p1"].phase is LifecyclePhase.AVAILABLE


async def test_per_user_isolated(any_state_store):
    future = datetime.now(UTC) + timedelta(seconds=60)
    await any_state_store.write(
        "p1", LLMState(phase=LifecyclePhase.COOLING, cooldown_until=future, fail_count=1), "alice"
    )
    alice = await any_state_store.read("alice")
    bob = await any_state_store.read("bob")
    unscoped = await any_state_store.read()
    assert "p1" in alice
    assert "p1" not in bob
    assert "p1" not in unscoped


async def test_user_id_none_unscoped(any_state_store):
    await any_state_store.write("shared", LLMState(fail_count=5))
    await any_state_store.write("alice-p1", LLMState(fail_count=1), "alice")
    result = await any_state_store.read()
    assert list(result) == ["shared"]
    assert result["shared"].fail_count == 5


async def test_offline_phase(any_state_store):
    state = LLMState(phase=LifecyclePhase.OFFLINE, fail_count=0)
    await any_state_store.write("p1", state)
    result = await any_state_store.read()
    assert result["p1"].phase is LifecyclePhase.OFFLINE
    assert result["p1"].cooldown_until is None


async def test_probing_phase(any_state_store):
    state = LLMState(phase=LifecyclePhase.PROBING, fail_count=0)
    await any_state_store.write("p1", state)
    result = await any_state_store.read()
    assert result["p1"].phase is LifecyclePhase.PROBING


async def test_extra_key_round_trips(any_state_store):
    """A future-proofing key not yet promoted to a named field survives write/read."""
    state = LLMState(phase=LifecyclePhase.AVAILABLE, fail_count=1, extra={"probe_attempts": 5})
    await any_state_store.write("p1", state)
    result = await any_state_store.read()
    assert result["p1"].extra == {"probe_attempts": 5}


# ── MongoDB: pre-migration documents (native BSON datetime cooldown_until) ───


async def test_mongodb_legacy_native_datetime_cooldown_reads_back(mongo_db):
    """Docs written by the pre-``to_dict()`` code stored cooldown_until as a native
    BSON date, which pymongo returns as a naive datetime (the client is never
    opened with tz_aware=True); read() must still reconcile it correctly.
    """
    store = llmbroker.mongodb.StateStore(mongo_db)
    future_aware = datetime.now(UTC) + timedelta(seconds=120)
    await mongo_db["llmbroker_state"].insert_one(
        {
            "llm_name": "legacy",
            "user_id": None,
            "phase": "cooling",
            "cooldown_until": future_aware,
            "fail_count": 1,
        },
    )
    try:
        result = await store.read()
        assert result["legacy"].phase is LifecyclePhase.COOLING
        assert result["legacy"].cooldown_until is not None
    finally:
        await mongo_db["llmbroker_state"].delete_many({"llm_name": "legacy"})

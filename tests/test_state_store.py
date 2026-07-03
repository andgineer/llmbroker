"""Backend-parametrized tests for the StateStore contract (sqlite, postgres, mongodb, redis)."""

import asyncio
from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import llmbroker.mongodb
import llmbroker.redis
import pytest

from llmbroker.models import LifecyclePhase, LLMState, QualitySummary


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


# ── Shared decayed summaries: apply_summary_delta / read_summaries / seed_summary ─


async def test_read_summaries_empty(any_state_store):
    assert await any_state_store.read_summaries() == {}


async def test_apply_summary_delta_inserts_when_absent(any_state_store):
    """Insert-if-absent: with no prior row, the delta values land verbatim."""
    await any_state_store.apply_summary_delta("x", "op", "quality", 1.0, 1.0, 0.8, 1.0, 1)
    result = (await any_state_store.read_summaries())[("x", "op", "quality")]
    assert result.weight == pytest.approx(1.0)
    assert result.weighted_good == pytest.approx(0.8)
    assert result.weight_sq == pytest.approx(1.0)
    assert result.count == 1


async def test_apply_summary_delta_matches_hand_computed_sequential_fold(any_state_store):
    """The server-side fold must equal QualitySummary.update() applied event by event."""
    d = 9 / 11
    events = [1.0, 1.0, 0.0, 1.0, 0.0]
    expected = QualitySummary()
    for v in events:
        expected.update(v, d)
        await any_state_store.apply_summary_delta("x", None, "transport", d, 1.0, v, 1.0, 1)
    actual = (await any_state_store.read_summaries())[("x", None, "transport")]
    assert actual.weight == pytest.approx(expected.weight, rel=1e-9)
    assert actual.weighted_good == pytest.approx(expected.weighted_good, rel=1e-9)
    assert actual.weight_sq == pytest.approx(expected.weight_sq, rel=1e-9)
    assert actual.count == expected.count


async def test_apply_summary_delta_batched_equals_event_by_event(any_state_store):
    """A client batching k local events into one delta must match applying them one by one."""
    d = 0.7
    events = [1.0, 0.0, 1.0, 1.0]
    k = len(events)

    for v in events:
        await any_state_store.apply_summary_delta("sequential", "op", "quality", d, 1.0, v, 1.0, 1)
    sequential = (await any_state_store.read_summaries())[("sequential", "op", "quality")]

    decay_pow = d**k
    add_weight = sum(d ** (k - 1 - i) for i in range(k))
    add_good = sum(d ** (k - 1 - i) * v for i, v in enumerate(events))
    add_weight_sq = sum((d * d) ** (k - 1 - i) for i in range(k))
    await any_state_store.apply_summary_delta(
        "batched", "op", "quality", decay_pow, add_weight, add_good, add_weight_sq, k
    )
    batched = (await any_state_store.read_summaries())[("batched", "op", "quality")]

    assert batched.weight == pytest.approx(sequential.weight, rel=1e-9)
    assert batched.weighted_good == pytest.approx(sequential.weighted_good, rel=1e-9)
    assert batched.weight_sq == pytest.approx(sequential.weight_sq, rel=1e-9)
    assert batched.count == sequential.count


async def test_concurrent_appliers_never_lose_events(any_state_store):
    """Concurrent asyncio-task appliers on the same key never clobber each other's count."""

    async def apply_one() -> None:
        await any_state_store.apply_summary_delta("x", "op", "quality", 0.99, 1.0, 1.0, 1.0, 1)

    await asyncio.gather(*(apply_one() for _ in range(20)))
    result = (await any_state_store.read_summaries())[("x", "op", "quality")]
    assert result.count == 20


async def test_seed_summary_idempotent_across_racing_instances(any_state_store):
    first = QualitySummary(weight=5.0, weighted_good=4.0, weight_sq=3.0, count=10)
    second = QualitySummary(weight=99.0, weighted_good=99.0, weight_sq=99.0, count=99)
    await any_state_store.seed_summary("x", "op", "quality", first)
    await any_state_store.seed_summary("x", "op", "quality", second)
    result = (await any_state_store.read_summaries())[("x", "op", "quality")]
    assert result == first


async def test_read_summaries_operation_none_distinct_from_named(any_state_store):
    await any_state_store.apply_summary_delta("x", None, "quality", 1.0, 1.0, 0.5, 1.0, 1)
    await any_state_store.apply_summary_delta("x", "summarize", "quality", 1.0, 1.0, 0.9, 1.0, 1)
    result = await any_state_store.read_summaries()
    assert result[("x", None, "quality")].weighted_good == pytest.approx(0.5)
    assert result[("x", "summarize", "quality")].weighted_good == pytest.approx(0.9)


async def test_summaries_isolated_per_user(any_state_store):
    await any_state_store.apply_summary_delta(
        "x", "op", "quality", 1.0, 1.0, 0.5, 1.0, 1, user_id="alice"
    )
    alice = await any_state_store.read_summaries("alice")
    bob = await any_state_store.read_summaries("bob")
    unscoped = await any_state_store.read_summaries()
    assert ("x", "op", "quality") in alice
    assert bob == {}
    assert unscoped == {}


async def test_redis_name_containing_field_separator_raises_value_error():
    """Regression: a name/operation containing the hash-field separator used to
    corrupt the encoded field key, and read_summaries would raise ValueError while
    decoding it — breaking reads for the whole scope, not just the offending row."""
    store = llmbroker.redis.StateStore(fakeredis.aioredis.FakeRedis(decode_responses=True))
    with pytest.raises(ValueError, match="must not contain"):
        await store.apply_summary_delta("bad\x1fname", "op", "quality", 1.0, 1.0, 0.5, 1.0, 1)


async def test_redis_operation_containing_field_separator_raises_value_error():
    store = llmbroker.redis.StateStore(fakeredis.aioredis.FakeRedis(decode_responses=True))
    with pytest.raises(ValueError, match="must not contain"):
        await store.apply_summary_delta("x", "bad\x1fop", "quality", 1.0, 1.0, 0.5, 1.0, 1)

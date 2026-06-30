"""Backend-parametrized tests for the QueryableTelemetryProtocol (sqlite, postgres, mongodb)."""

from datetime import UTC, datetime, timedelta

import pytest

from llmbroker.models import Call, CallStatus, Usage


def _call(call_id="c1", llm_name="p1", user_id=None, **kw):
    return Call(
        id=call_id,
        llm_name=llm_name,
        operation=None,
        trace_id=None,
        status=CallStatus.OK,
        user_id=user_id,
        **kw,
    )


async def test_record_and_calls(queryable_telemetry):
    await queryable_telemetry.record(_call("c1"))
    calls = await queryable_telemetry.calls(limit=10)
    assert len(calls) == 1
    assert calls[0].id == "c1"


async def test_calls_respects_limit(queryable_telemetry):
    for i in range(5):
        await queryable_telemetry.record(_call(f"c{i}"))
    calls = await queryable_telemetry.calls(limit=3)
    assert len(calls) == 3


async def test_calls_user_id_scoping(queryable_telemetry):
    await queryable_telemetry.record(_call("ca", user_id="alice"))
    await queryable_telemetry.record(_call("cb", user_id="bob"))
    alice_calls = await queryable_telemetry.calls(limit=10, user_id="alice")
    assert len(alice_calls) == 1
    assert alice_calls[0].id == "ca"


async def test_calls_roundtrips_usage_with_extra(queryable_telemetry):
    usage = Usage(prompt_tokens=10, completion_tokens=20, total_tokens=30, extra={"cached": 5})
    await queryable_telemetry.record(_call(usage=usage))
    calls = await queryable_telemetry.calls(limit=1)
    u = calls[0].usage
    assert u is not None
    assert u.prompt_tokens == 10
    assert u.extra == {"cached": 5}


async def test_calls_none_usage_roundtrips_as_none(queryable_telemetry):
    await queryable_telemetry.record(_call())
    calls = await queryable_telemetry.calls(limit=1)
    assert calls[0].usage is None


async def test_record_quality_updates_score(queryable_telemetry):
    await queryable_telemetry.record(_call())
    await queryable_telemetry.record_quality("c1", 0.8)
    calls = await queryable_telemetry.calls(limit=1)
    assert calls[0].quality_score == pytest.approx(0.8)


async def test_record_quality_missing_raises_key_error(queryable_telemetry):
    with pytest.raises(KeyError):
        await queryable_telemetry.record_quality("nonexistent", 1.0)


async def test_purge_calls_removes_old_records(queryable_telemetry):
    await queryable_telemetry.record(_call())
    deleted = await queryable_telemetry.purge_calls(before=datetime.now(UTC) + timedelta(seconds=5))
    assert deleted == 1
    assert await queryable_telemetry.calls(limit=10) == []


async def test_purge_calls_returns_zero_when_nothing_qualifies(queryable_telemetry):
    await queryable_telemetry.record(_call())
    deleted = await queryable_telemetry.purge_calls(before=datetime.now(UTC) - timedelta(days=1))
    assert deleted == 0


async def test_metrics_counts_calls_per_llm(queryable_telemetry):
    await queryable_telemetry.record(_call("c1", llm_name="llm1"))
    await queryable_telemetry.record(_call("c2", llm_name="llm1"))
    await queryable_telemetry.record(_call("c3", llm_name="llm2"))
    m = await queryable_telemetry.metrics()
    assert m["llm1"].call_count == 2
    assert m["llm2"].call_count == 1


async def test_metrics_since_filters_past_calls(queryable_telemetry):
    await queryable_telemetry.record(_call())
    m = await queryable_telemetry.metrics(since=datetime.now(UTC) + timedelta(seconds=5))
    assert m == {}


async def test_metrics_user_id_scoping(queryable_telemetry):
    await queryable_telemetry.record(_call("ca", llm_name="llm1", user_id="alice"))
    await queryable_telemetry.record(_call("cb", llm_name="llm1", user_id="bob"))
    m_alice = await queryable_telemetry.metrics(user_id="alice")
    m_bob = await queryable_telemetry.metrics(user_id="bob")
    m_unscoped = await queryable_telemetry.metrics()
    assert m_alice["llm1"].call_count == 1
    assert m_bob["llm1"].call_count == 1
    assert m_unscoped == {}

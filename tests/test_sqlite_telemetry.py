"""Unit tests for sqlite.Telemetry: record_quality, calls, purge_calls, metrics."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

import llmbroker.sqlite
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


def test_record_quality_updates_score(tmp_path):
    db = str(tmp_path / "t.db")
    tel = llmbroker.sqlite.Telemetry(db)

    async def run():
        await tel.record(_call())
        await tel.record_quality("c1", 0.8)
        calls = await tel.calls(limit=10)
        assert calls[0].quality_score == 0.8

    asyncio.run(run())


def test_record_quality_missing_id_raises_key_error(tmp_path):
    db = str(tmp_path / "t.db")
    tel = llmbroker.sqlite.Telemetry(db)

    async def run():
        with pytest.raises(KeyError):
            await tel.record_quality("nonexistent", 1.0)

    asyncio.run(run())


def test_calls_returns_all_records(tmp_path):
    db = str(tmp_path / "t.db")
    tel = llmbroker.sqlite.Telemetry(db)

    async def run():
        await tel.record(_call("c1"))
        await tel.record(_call("c2"))
        calls = await tel.calls(limit=10)
        assert {c.id for c in calls} == {"c1", "c2"}

    asyncio.run(run())


def test_calls_respects_limit(tmp_path):
    db = str(tmp_path / "t.db")
    tel = llmbroker.sqlite.Telemetry(db)

    async def run():
        for i in range(5):
            await tel.record(_call(f"c{i}"))
        calls = await tel.calls(limit=3)
        assert len(calls) == 3

    asyncio.run(run())


def test_calls_user_id_scoping(tmp_path):
    db = str(tmp_path / "t.db")
    tel = llmbroker.sqlite.Telemetry(db)

    async def run():
        await tel.record(_call("ca", user_id="alice"))
        await tel.record(_call("cb", user_id="bob"))
        alice_calls = await tel.calls(limit=10, user_id="alice")
        assert len(alice_calls) == 1
        assert alice_calls[0].id == "ca"

    asyncio.run(run())


def test_calls_roundtrips_usage_with_extra(tmp_path):
    db = str(tmp_path / "t.db")
    tel = llmbroker.sqlite.Telemetry(db)
    usage = Usage(prompt_tokens=10, completion_tokens=20, total_tokens=30, extra={"cached": 5})

    async def run():
        await tel.record(_call(usage=usage))
        calls = await tel.calls(limit=1)
        u = calls[0].usage
        assert u is not None
        assert u.prompt_tokens == 10
        assert u.completion_tokens == 20
        assert u.total_tokens == 30
        assert u.extra == {"cached": 5}

    asyncio.run(run())


def test_calls_none_usage_roundtrips_as_none(tmp_path):
    db = str(tmp_path / "t.db")
    tel = llmbroker.sqlite.Telemetry(db)

    async def run():
        await tel.record(_call())
        calls = await tel.calls(limit=1)
        assert calls[0].usage is None

    asyncio.run(run())


def test_purge_calls_removes_old_records(tmp_path):
    db = str(tmp_path / "t.db")
    tel = llmbroker.sqlite.Telemetry(db)

    async def run():
        await tel.record(_call())
        deleted = await tel.purge_calls(before=datetime.now(UTC) + timedelta(seconds=5))
        assert deleted == 1
        assert await tel.calls(limit=10) == []

    asyncio.run(run())


def test_purge_calls_returns_zero_when_nothing_qualifies(tmp_path):
    db = str(tmp_path / "t.db")
    tel = llmbroker.sqlite.Telemetry(db)

    async def run():
        await tel.record(_call())
        deleted = await tel.purge_calls(before=datetime.now(UTC) - timedelta(days=1))
        assert deleted == 0

    asyncio.run(run())


def test_metrics_counts_calls_per_llm(tmp_path):
    db = str(tmp_path / "t.db")
    tel = llmbroker.sqlite.Telemetry(db)

    async def run():
        await tel.record(_call("c1", llm_name="llm1"))
        await tel.record(_call("c2", llm_name="llm1"))
        await tel.record(_call("c3", llm_name="llm2"))
        m = await tel.metrics()
        assert m["llm1"].call_count == 2
        assert m["llm2"].call_count == 1

    asyncio.run(run())


def test_metrics_since_filters_past_calls(tmp_path):
    db = str(tmp_path / "t.db")
    tel = llmbroker.sqlite.Telemetry(db)

    async def run():
        await tel.record(_call())
        since = datetime.now(UTC) + timedelta(seconds=5)
        m = await tel.metrics(since=since)
        assert m == {}

    asyncio.run(run())

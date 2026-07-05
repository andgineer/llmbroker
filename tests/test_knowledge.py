"""Tests for the standalone knowledge stores: InMemoryKnowledge, FileKnowledge."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from llmbroker.models import Call, CallStatus
from llmbroker.standalone.knowledge import FileKnowledge, InMemoryKnowledge

_TODAY = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
_YESTERDAY = _TODAY - timedelta(days=1)


def _call(call_id="c1", llm_name="p1", ts=None):
    return Call(
        id=call_id,
        llm_name=llm_name,
        operation="test",
        trace_id=None,
        status=CallStatus.OK,
        ts=ts if ts is not None else _TODAY,
        http_status=200,
        latency_ms=100,
        error_detail=None,
        usage=None,
    )


def test_in_memory_knowledge_record_does_not_raise():
    asyncio.run(InMemoryKnowledge().record(_call()))


def test_in_memory_knowledge_record_quality_does_not_raise():
    asyncio.run(InMemoryKnowledge().record_quality("p1", "summarize", 1.0, call_id="c1"))


def test_in_memory_knowledge_disabled_map_is_in_memory_only():
    async def run():
        k = InMemoryKnowledge()
        await k.seed_disabled(["p1", "p2"])
        assert await k.disabled_map() == {"p1": False, "p2": False}
        await k.set_disabled("p1", True)
        assert await k.get_disabled("p1") is True
        assert await k.get_disabled("p2") is False

    asyncio.run(run())


def test_file_knowledge_record_writes_day_file(tmp_path):
    asyncio.run(FileKnowledge(tmp_path).record(_call(ts=_TODAY)))
    path = tmp_path / "calls" / f"{_TODAY.date().isoformat()}.jsonl"
    line = json.loads(path.read_text())
    assert line["kind"] == "call"
    assert line["id"] == "c1"
    assert line["llm_name"] == "p1"
    assert line["status"] == "ok"
    assert line["http_status"] == 200


def test_file_knowledge_record_quality_writes_line(tmp_path):
    asyncio.run(FileKnowledge(tmp_path).record_quality("p1", "summarize", 0.8, call_id="c1"))
    day_files = list((tmp_path / "calls").glob("*.jsonl"))
    assert len(day_files) == 1
    line = json.loads(day_files[0].read_text())
    assert line["kind"] == "quality"
    assert line["llm_name"] == "p1"
    assert line["operation"] == "summarize"
    assert line["call_id"] == "c1"
    assert line["quality_score"] == 0.8
    assert "status" not in line  # None fields are dropped at serialization


def test_file_knowledge_record_appends_multiple_same_day(tmp_path):
    k = FileKnowledge(tmp_path)
    asyncio.run(k.record(_call("c1", ts=_TODAY)))
    asyncio.run(k.record(_call("c2", ts=_TODAY)))
    path = tmp_path / "calls" / f"{_TODAY.date().isoformat()}.jsonl"
    lines = [json.loads(l) for l in path.read_text().strip().splitlines()]
    assert len(lines) == 2
    assert lines[0]["id"] == "c1"
    assert lines[1]["id"] == "c2"


def test_file_knowledge_splits_by_day(tmp_path):
    k = FileKnowledge(tmp_path)
    asyncio.run(k.record(_call("c1", ts=_YESTERDAY)))
    asyncio.run(k.record(_call("c2", ts=_TODAY)))
    assert (tmp_path / "calls" / f"{_YESTERDAY.date().isoformat()}.jsonl").exists()
    assert (tmp_path / "calls" / f"{_TODAY.date().isoformat()}.jsonl").exists()


def test_file_knowledge_calls_reads_newest_first_across_days(tmp_path):
    k = FileKnowledge(tmp_path)

    async def run():
        await k.record(_call("c1", ts=_YESTERDAY))
        await k.record(_call("c2", ts=_TODAY))
        return await k.calls(limit=10)

    calls = asyncio.run(run())
    assert [c.id for c in calls] == ["c2", "c1"]


def test_file_knowledge_calls_respects_limit(tmp_path):
    k = FileKnowledge(tmp_path)

    async def run():
        for i in range(5):
            await k.record(_call(f"c{i}", ts=_TODAY))
        return await k.calls(limit=2)

    calls = asyncio.run(run())
    assert len(calls) == 2
    assert [c.id for c in calls] == ["c4", "c3"]


def test_file_knowledge_calls_scope_filter(tmp_path):
    k = FileKnowledge(tmp_path)

    async def run():
        await k.record(replace(_call("ca"), scope="alice"))
        await k.record(replace(_call("cb"), scope="bob"))
        return await k.calls(limit=10, scope="alice")

    calls = asyncio.run(run())
    assert [c.id for c in calls] == ["ca"]


def test_file_knowledge_disabled_map_persists_to_yaml_file(tmp_path):
    async def run():
        k = FileKnowledge(tmp_path)
        await k.seed_disabled(["p1"])
        await k.set_disabled("p1", True)
        # A fresh instance reading the same directory sees the persisted verdict.
        k2 = FileKnowledge(tmp_path)
        return await k2.get_disabled("p1")

    assert asyncio.run(run()) is True
    assert (tmp_path / "disabled.yml").exists()


def test_file_knowledge_seed_disabled_never_overwrites_existing_value(tmp_path):
    k = FileKnowledge(tmp_path)

    async def run():
        await k.set_disabled("p1", True)
        await k.seed_disabled(["p1", "p2"])
        return await k.disabled_map()

    result = asyncio.run(run())
    assert result == {"p1": True, "p2": False}


def test_file_knowledge_purges_day_files_older_than_retention(tmp_path):
    old_ts = datetime.now(UTC) - timedelta(days=100)
    recent_ts = datetime.now(UTC) - timedelta(days=1)
    k = FileKnowledge(tmp_path, retention=timedelta(days=90))

    async def run():
        await k.record(_call("old", ts=old_ts))
        await k.record(_call("recent", ts=recent_ts))

    asyncio.run(run())
    remaining = {c.id for c in asyncio.run(k.calls(limit=10))}
    assert remaining == {"recent"}


def test_file_knowledge_retention_purge_is_debounced(tmp_path):
    """A second write within the debounce window does not re-run the purge scan —
    verified indirectly: an old file written after the first purge survives until
    the debounce interval elapses again."""
    old_ts = datetime.now(UTC) - timedelta(days=100)
    k = FileKnowledge(tmp_path, retention=timedelta(days=90))

    async def run():
        await k.record(_call("triggers-purge", ts=datetime.now(UTC)))  # sets _last_purge
        await k.record(_call("old", ts=old_ts))  # written after purge already ran once

    asyncio.run(run())
    remaining = {c.id for c in asyncio.run(k.calls(limit=10))}
    assert "old" in remaining  # not purged — debounce window hasn't elapsed

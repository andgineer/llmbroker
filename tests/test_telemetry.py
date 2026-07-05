"""Tests for telemetry batteries: Telemetry, NoTelemetry, JsonlTelemetry."""

import asyncio
import json
from dataclasses import replace

from llmbroker.models import Call, CallStatus
from llmbroker.standalone.telemetry import JsonlTelemetry, NoTelemetry, Telemetry


def _call(call_id="c1", llm_name="p1"):
    return Call(
        id=call_id,
        llm_name=llm_name,
        operation="test",
        trace_id=None,
        status=CallStatus.OK,
        http_status=200,
        latency_ms=100,
        error_detail=None,
        usage=None,
    )


def test_no_telemetry_record_does_not_raise():
    asyncio.run(NoTelemetry().record(_call()))


def test_no_telemetry_record_quality_does_not_raise():
    asyncio.run(NoTelemetry().record_quality("p1", "summarize", 1.0, call_id="c1"))


def test_no_telemetry_disabled_map_is_in_memory_only():
    async def run():
        tel = NoTelemetry()
        await tel.seed_disabled(["p1", "p2"])
        assert await tel.disabled_map() == {"p1": False, "p2": False}
        await tel.set_disabled("p1", True)
        assert await tel.get_disabled("p1") is True
        assert await tel.get_disabled("p2") is False

    asyncio.run(run())


def test_telemetry_disabled_map_is_in_memory_only():
    async def run():
        tel = Telemetry()
        await tel.set_disabled("p1", True)
        assert await tel.disabled_map() == {"p1": True}
        await tel.set_disabled("p1", False)
        assert await tel.get_disabled("p1") is False

    asyncio.run(run())


def test_jsonl_record_writes_line(tmp_path):
    path = tmp_path / "calls.jsonl"
    asyncio.run(JsonlTelemetry(path).record(_call()))
    line = json.loads(path.read_text())
    assert line["kind"] == "call"
    assert line["id"] == "c1"
    assert line["llm_name"] == "p1"
    assert line["status"] == "ok"
    assert line["http_status"] == 200


def test_jsonl_record_quality_writes_line(tmp_path):
    path = tmp_path / "calls.jsonl"
    asyncio.run(JsonlTelemetry(path).record_quality("p1", "summarize", 0.8, call_id="c1"))
    line = json.loads(path.read_text())
    assert line["kind"] == "quality"
    assert line["llm_name"] == "p1"
    assert line["operation"] == "summarize"
    assert line["call_id"] == "c1"
    assert line["quality_score"] == 0.8
    assert "status" not in line  # None fields are dropped at serialization


def test_jsonl_record_appends_multiple(tmp_path):
    path = tmp_path / "calls.jsonl"
    tel = JsonlTelemetry(path)
    asyncio.run(tel.record(_call("c1")))
    asyncio.run(tel.record(_call("c2")))
    lines = [json.loads(l) for l in path.read_text().strip().splitlines()]
    assert len(lines) == 2
    assert lines[0]["id"] == "c1"
    assert lines[1]["id"] == "c2"


def test_jsonl_calls_reads_newest_first(tmp_path):
    path = tmp_path / "calls.jsonl"
    tel = JsonlTelemetry(path)

    async def run():
        await tel.record(_call("c1"))
        await tel.record(_call("c2"))
        return await tel.calls(limit=10)

    calls = asyncio.run(run())
    assert [c.id for c in calls] == ["c2", "c1"]


def test_jsonl_calls_respects_limit(tmp_path):
    path = tmp_path / "calls.jsonl"
    tel = JsonlTelemetry(path)

    async def run():
        for i in range(5):
            await tel.record(_call(f"c{i}"))
        return await tel.calls(limit=2)

    calls = asyncio.run(run())
    assert len(calls) == 2
    assert [c.id for c in calls] == ["c4", "c3"]


def test_jsonl_calls_scope_filter(tmp_path):
    path = tmp_path / "calls.jsonl"
    tel = JsonlTelemetry(path)

    async def run():
        await tel.record(replace(_call("ca"), scope="alice"))
        await tel.record(replace(_call("cb"), scope="bob"))
        return await tel.calls(limit=10, scope="alice")

    calls = asyncio.run(run())
    assert [c.id for c in calls] == ["ca"]


def test_jsonl_disabled_map_persists_to_sibling_file(tmp_path):
    path = tmp_path / "calls.jsonl"

    async def run():
        tel = JsonlTelemetry(path)
        await tel.seed_disabled(["p1"])
        await tel.set_disabled("p1", True)
        # A fresh instance reading the same sibling file sees the persisted verdict.
        tel2 = JsonlTelemetry(path)
        return await tel2.get_disabled("p1")

    assert asyncio.run(run()) is True
    assert (tmp_path / "calls.disabled.json").exists()


def test_jsonl_seed_disabled_never_overwrites_existing_value(tmp_path):
    path = tmp_path / "calls.jsonl"

    async def run():
        tel = JsonlTelemetry(path)
        await tel.set_disabled("p1", True)
        await tel.seed_disabled(["p1", "p2"])
        return await tel.disabled_map()

    result = asyncio.run(run())
    assert result == {"p1": True, "p2": False}

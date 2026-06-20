"""Tests for telemetry batteries: NoTelemetry, JsonlTelemetry."""

import asyncio
import json

from llmbroker.models import Call, CallStatus
from llmbroker.telemetry import JsonlTelemetry, NoTelemetry


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
    asyncio.run(NoTelemetry().record_quality("c1", 1.0))


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
    asyncio.run(JsonlTelemetry(path).record_quality("c1", 0.8))
    line = json.loads(path.read_text())
    assert line["kind"] == "quality"
    assert line["call_id"] == "c1"
    assert line["score"] == 0.8


def test_jsonl_record_appends_multiple(tmp_path):
    path = tmp_path / "calls.jsonl"
    tel = JsonlTelemetry(path)
    asyncio.run(tel.record(_call("c1")))
    asyncio.run(tel.record(_call("c2")))
    lines = [json.loads(l) for l in path.read_text().strip().splitlines()]
    assert len(lines) == 2
    assert lines[0]["id"] == "c1"
    assert lines[1]["id"] == "c2"

"""Telemetry port protocols and the zero-dependency batteries.

``Telemetry()`` (log, default) and ``NoTelemetry()`` implement only the
minimal contract. ``JsonlTelemetry(path)`` appends JSON lines. ``record_quality``
on the log/jsonl batteries appends a distinct quality record, never a Call.
Queryable backends (sqlite, …) implement ``QueryableTelemetryProtocol``.
"""

import json
import logging
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from llmbroker.models import Call, LLMMetrics

logger = logging.getLogger("llmbroker.telemetry")


class TelemetryProtocol(Protocol):
    async def record(self, call: Call) -> None: ...
    async def record_quality(self, call_id: str, score: float) -> None: ...


@runtime_checkable
class QueryableTelemetryProtocol(TelemetryProtocol, Protocol):
    async def metrics(self, *, since: datetime | None = None) -> dict[str, LLMMetrics]: ...
    async def calls(self, *, limit: int) -> list[Call]: ...
    async def purge_calls(self, *, before: datetime) -> int: ...


class Telemetry:
    """Default telemetry — emits each call to Python logging."""

    async def record(self, call: Call) -> None:
        logger.info(
            "llm call id=%s llm=%s operation=%s status=%s http=%s latency=%sms",
            call.id,
            call.llm_name,
            call.operation,
            call.status.value,
            call.http_status,
            call.latency_ms,
        )

    async def record_quality(self, call_id: str, score: float) -> None:
        logger.info("quality call=%s score=%s", call_id, score)


class NoTelemetry:
    """Explicit no-op telemetry opt-out."""

    async def record(self, _call: Call) -> None:
        return

    async def record_quality(self, _call_id: str, _score: float) -> None:
        return


def _call_to_jsonable(call: Call) -> dict:
    data = asdict(call)
    data["status"] = call.status.value
    return data


class JsonlTelemetry:
    """Append-only JSON-lines telemetry."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    async def record(self, call: Call) -> None:
        line = json.dumps({"kind": "call", **_call_to_jsonable(call)})
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    async def record_quality(self, call_id: str, score: float) -> None:
        line = json.dumps({"kind": "quality", "call_id": call_id, "score": score})
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

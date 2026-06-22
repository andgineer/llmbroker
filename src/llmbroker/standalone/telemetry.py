"""Logging and JSON-lines telemetry — no external backend.

``Telemetry()`` (log, default) and ``NoTelemetry()`` implement only the minimal
contract. ``JsonlTelemetry(path)`` appends JSON lines. ``record_quality`` on
these appends a distinct quality record, never a Call.
"""

import asyncio
import json
import logging
from dataclasses import asdict
from pathlib import Path

from llmbroker.models import Call

logger = logging.getLogger("llmbroker.telemetry")


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

    def _append(self, line: str) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    async def record(self, call: Call) -> None:
        line = json.dumps({"kind": "call", **_call_to_jsonable(call)})
        await asyncio.to_thread(self._append, line)

    async def record_quality(self, call_id: str, score: float) -> None:
        line = json.dumps({"kind": "quality", "call_id": call_id, "score": score})
        await asyncio.to_thread(self._append, line)

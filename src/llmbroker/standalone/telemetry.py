"""Logging and JSON-lines telemetry — no external backend.

``Telemetry()`` (log, default) and ``NoTelemetry()`` implement only the minimal
contract; their disabled-verdict map is in-memory only (session-scoped).
``JsonlTelemetry(path)`` appends JSON lines — a quality record is its own
line, never an update to an existing one — and persists the disabled map to a
sibling JSON file. It is queryable, so the journal rebuild can warm-start and
stay live from a plain file, same as a DB backend.
"""

import asyncio
import json
import logging
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from llmbroker.models import Call, CallStatus, Usage

logger = logging.getLogger("llmbroker.telemetry")


def _new_quality_call(
    llm_name: str,
    operation: str | None,
    score: float,
    call_id: str | None,
) -> Call:
    return Call(
        id=str(uuid.uuid4()),
        llm_name=llm_name,
        operation=operation,
        trace_id=None,
        status=None,
        kind="quality",
        ts=datetime.now(UTC),
        quality_score=score,
        call_id=call_id,
    )


class Telemetry:
    """Default telemetry — emits each call to Python logging; disabled verdicts
    live only in process memory."""

    def __init__(self) -> None:
        self._disabled: dict[str, bool] = {}

    async def record(self, call: Call) -> None:
        logger.info(
            "llm call id=%s llm=%s operation=%s status=%s http=%s latency=%sms",
            call.id,
            call.llm_name,
            call.operation,
            call.status.value if call.status is not None else None,
            call.http_status,
            call.latency_ms,
        )

    async def record_quality(
        self,
        llm_name: str,
        operation: str | None,
        score: float,
        *,
        call_id: str | None = None,  # noqa: ARG002
    ) -> None:
        logger.info("quality llm=%s operation=%s score=%s", llm_name, operation, score)

    async def get_disabled(self, name: str) -> bool:
        return self._disabled.get(name, False)

    async def set_disabled(self, name: str, flag: bool) -> None:  # noqa: FBT001
        self._disabled[name] = flag

    async def seed_disabled(self, names: list[str]) -> None:
        for name in names:
            self._disabled.setdefault(name, False)

    async def disabled_map(self) -> dict[str, bool]:
        return dict(self._disabled)


class NoTelemetry:
    """Explicit no-op telemetry opt-out; disabled verdicts live only in process memory."""

    def __init__(self) -> None:
        self._disabled: dict[str, bool] = {}

    async def record(self, _call: Call) -> None:
        return

    async def record_quality(
        self,
        _llm_name: str,
        _operation: str | None,
        _score: float,
        *,
        call_id: str | None = None,  # noqa: ARG002
    ) -> None:
        return

    async def get_disabled(self, name: str) -> bool:
        return self._disabled.get(name, False)

    async def set_disabled(self, name: str, flag: bool) -> None:  # noqa: FBT001
        self._disabled[name] = flag

    async def seed_disabled(self, names: list[str]) -> None:
        for name in names:
            self._disabled.setdefault(name, False)

    async def disabled_map(self) -> dict[str, bool]:
        return dict(self._disabled)


def _call_to_jsonable(call: Call) -> dict:
    data = asdict(call)
    data["status"] = call.status.value if call.status is not None else None
    data["ts"] = call.ts.isoformat() if call.ts is not None else None
    data["cooldown_until"] = call.cooldown_until.isoformat() if call.cooldown_until else None
    return {k: v for k, v in data.items() if v is not None}


def _call_from_jsonable(d: dict) -> Call:
    usage_raw = d.get("usage")
    usage = Usage(**usage_raw) if usage_raw else None
    status = d.get("status")
    ts_raw = d.get("ts")
    cooldown_raw = d.get("cooldown_until")
    return Call(
        id=d["id"],
        llm_name=d["llm_name"],
        operation=d.get("operation"),
        trace_id=d.get("trace_id"),
        status=CallStatus(status) if status is not None else None,
        kind=d.get("kind", "call"),
        ts=datetime.fromisoformat(ts_raw) if ts_raw else None,
        http_status=d.get("http_status"),
        latency_ms=d.get("latency_ms"),
        error_detail=d.get("error_detail"),
        usage=usage,
        quality_score=d.get("quality_score"),
        call_id=d.get("call_id"),
        scope=d.get("scope"),
        cooldown_until=datetime.fromisoformat(cooldown_raw) if cooldown_raw else None,
        key_hash=d.get("key_hash"),
    )


class JsonlTelemetry:
    """Append-only JSON-lines call journal, plus a JSON-file disabled-verdict map."""

    def __init__(self, path: str | Path, *, disabled_path: str | Path | None = None) -> None:
        self._path = Path(path)
        self._disabled_path = (
            Path(disabled_path)
            if disabled_path is not None
            else self._path.parent / f"{self._path.stem}.disabled.json"
        )

    def _append(self, line: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    async def record(self, call: Call) -> None:
        line = json.dumps(_call_to_jsonable(call))
        await asyncio.to_thread(self._append, line)

    async def record_quality(
        self,
        llm_name: str,
        operation: str | None,
        score: float,
        *,
        call_id: str | None = None,
    ) -> None:
        await self.record(_new_quality_call(llm_name, operation, score, call_id))

    def _read_all(self) -> list[Call]:
        if not self._path.exists():
            return []
        calls: list[Call] = []
        with self._path.open(encoding="utf-8") as fh:
            for raw_line in fh:
                stripped = raw_line.strip()
                if stripped:
                    calls.append(_call_from_jsonable(json.loads(stripped)))
        return calls

    async def calls(self, *, limit: int, scope: str | None = None) -> list[Call]:
        """Newest-first tail of the journal, both kinds interleaved — unfiltered by scope
        (learning is global); ``scope`` is accepted for the host-facing filter only."""
        rows = await asyncio.to_thread(self._read_all)
        if scope is not None:
            rows = [r for r in rows if r.scope == scope]
        rows.reverse()
        return rows[:limit]

    async def purge_calls(self, *, before: datetime) -> int:
        rows = await asyncio.to_thread(self._read_all)
        kept = [r for r in rows if r.ts is None or r.ts >= before]
        removed = len(rows) - len(kept)
        if removed:
            lines = [json.dumps(_call_to_jsonable(r)) for r in kept]
            await asyncio.to_thread(
                self._path.write_text,
                "".join(line + "\n" for line in lines),
                "utf-8",
            )
        return removed

    def _read_disabled(self) -> dict[str, bool]:
        if not self._disabled_path.exists():
            return {}
        return json.loads(self._disabled_path.read_text(encoding="utf-8"))

    def _write_disabled(self, data: dict[str, bool]) -> None:
        self._disabled_path.parent.mkdir(parents=True, exist_ok=True)
        self._disabled_path.write_text(json.dumps(data), encoding="utf-8")

    async def get_disabled(self, name: str) -> bool:
        data = await asyncio.to_thread(self._read_disabled)
        return bool(data.get(name, False))

    async def set_disabled(self, name: str, flag: bool) -> None:  # noqa: FBT001
        data = await asyncio.to_thread(self._read_disabled)
        data[name] = flag
        await asyncio.to_thread(self._write_disabled, data)

    async def seed_disabled(self, names: list[str]) -> None:
        data = await asyncio.to_thread(self._read_disabled)
        changed = False
        for name in names:
            if name not in data:
                data[name] = False
                changed = True
        if changed:
            await asyncio.to_thread(self._write_disabled, data)

    async def disabled_map(self) -> dict[str, bool]:
        return await asyncio.to_thread(self._read_disabled)

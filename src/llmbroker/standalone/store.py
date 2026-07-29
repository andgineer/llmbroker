"""File-backed and in-memory stores — no external backend.

``InMemoryStore()`` implements only the minimal contract and keeps its
disabled-verdict map in process memory (session-scoped learning). It is
llmbroker's internal subsystem, not application logging — a host that wants
logs uses ``logging`` itself.

``FileStore(directory)`` is the default persistent store: a day-split
JSON-lines call journal (``<directory>/calls/YYYY-MM-DD.jsonl``, chosen by
each record's UTC date — pure storage layout, not aggregation, since rebuild
needs raw per-record scores and a quality record can rate a call from an
earlier day) plus a YAML admin disabled-verdict map
(``<directory>/disabled.yml``, meant for hand-editing). It self-purges call
records older than ``retention`` by unlinking whole expired day files — no
rewrite, no race with concurrent appends — checked at most once per hour on
write activity. The disabled map is never purged.
"""

import asyncio
import json
import time
import uuid
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import yaml

from llmbroker.models import Call, CallStatus, Usage, check_limit, to_utc, with_utc_timestamps

_DEFAULT_RETENTION = timedelta(days=90)
_PURGE_INTERVAL_SECONDS = 3600.0
_DISABLED_HEADER = "# llmbroker: admin verdicts; values are yours, names are seeded automatically\n"


def _new_quality_call(
    llm_name: str,
    operation: str | None,
    score: float,
    call_id: str | None,
    scope: str | None,
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
        scope=scope,
    )


class InMemoryStore:
    """Explicit in-memory opt-out — no persistence, session-scoped learning;
    disabled verdicts live only in process memory."""

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
        scope: str | None = None,  # noqa: ARG002
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


class FileStore:
    """Day-split JSONL call journal plus a YAML disabled-verdict map, under one directory."""

    def __init__(self, directory: str | Path, *, retention: timedelta = _DEFAULT_RETENTION) -> None:
        self._dir = Path(directory)
        self._calls_dir = self._dir / "calls"
        self._disabled_path = self._dir / "disabled.yml"
        self._retention = retention
        self._last_purge = float("-inf")

    def _day_path(self, ts: datetime) -> Path:
        """UTC date, not the record's own offset: the ``since`` bound skips whole
        files by name, so a file must never hold a row outside its named UTC day."""
        return self._calls_dir / f"{ts.astimezone(UTC).date().isoformat()}.jsonl"

    def _append(self, call: Call) -> None:
        path = self._day_path(call.ts)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(_call_to_jsonable(call))
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    async def record(self, call: Call) -> None:
        await asyncio.to_thread(self._append, with_utc_timestamps(call))
        await self._maybe_purge()

    async def record_quality(
        self,
        llm_name: str,
        operation: str | None,
        score: float,
        *,
        call_id: str | None = None,
        scope: str | None = None,
    ) -> None:
        await self.record(_new_quality_call(llm_name, operation, score, call_id, scope))

    def _day_files_newest_first(self) -> list[Path]:
        if not self._calls_dir.exists():
            return []
        return sorted(self._calls_dir.glob("*.jsonl"), reverse=True)

    def _read_tail(
        self,
        limit: int,
        *,
        scope: str | None,
        since: datetime | None,
        kind: str | None,
        operation: str | None,
    ) -> list[Call]:
        result: list[Call] = []
        for path in self._day_files_newest_first():
            if since is not None and self._file_is_wholly_before(path, since):
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            for raw_line in reversed(lines):
                stripped = raw_line.strip()
                if not stripped:
                    continue
                call = _call_from_jsonable(json.loads(stripped))
                if scope is not None and call.scope != scope:
                    continue
                if kind is not None and call.kind != kind:
                    continue
                if operation is not None and call.operation != operation:
                    continue
                if since is not None and (call.ts is None or call.ts < since):
                    continue
                result.append(call)
                if len(result) >= limit:
                    return result
        return result

    @staticmethod
    def _file_is_wholly_before(path: Path, since: datetime) -> bool:
        """A day file's newest possible record is the last instant of its UTC date, so
        a file whose whole day precedes ``since`` is skipped without being read."""
        try:
            file_date = date.fromisoformat(path.stem)
        except ValueError:
            return False
        return file_date < since.date()

    async def calls(
        self,
        *,
        limit: int,
        scope: str | None = None,
        since: datetime | None = None,
        kind: str | None = None,
        operation: str | None = None,
    ) -> list[Call]:
        """Newest-first tail of the journal, both kinds interleaved unless ``kind``
        narrows them — unfiltered by scope (learning is global); ``scope`` is accepted
        for the host-facing filter only. ``since`` must be timezone-aware and bounds
        the timestamp inclusively."""
        check_limit(limit)
        bound = to_utc(since, "since") if since is not None else None
        return await asyncio.to_thread(
            self._read_tail,
            limit,
            scope=scope,
            since=bound,
            kind=kind,
            operation=operation,
        )

    def _purge_old_day_files(self) -> None:
        cutoff = (datetime.now(UTC) - self._retention).date()
        for path in self._day_files_newest_first():
            try:
                file_date = date.fromisoformat(path.stem)
            except ValueError:
                continue
            if file_date < cutoff:
                path.unlink(missing_ok=True)

    async def _maybe_purge(self) -> None:
        now = time.monotonic()
        if now - self._last_purge < _PURGE_INTERVAL_SECONDS:
            return
        self._last_purge = now
        await asyncio.to_thread(self._purge_old_day_files)

    def _read_disabled(self) -> dict[str, bool]:
        if not self._disabled_path.exists():
            return {}
        data = yaml.safe_load(self._disabled_path.read_text(encoding="utf-8"))
        return dict(data) if data else {}

    def _write_disabled(self, data: dict[str, bool]) -> None:
        self._disabled_path.parent.mkdir(parents=True, exist_ok=True)
        body = yaml.safe_dump(data, sort_keys=True)
        self._disabled_path.write_text(_DISABLED_HEADER + body, encoding="utf-8")

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

"""SQLite-backed queryable knowledge store + admin disabled-map over one DB file."""

import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

from llmbroker.models import Call, CallStatus, LLMMetrics, Usage
from llmbroker.sqlite.schema import ensure_schema

_DEFAULT_RETENTION = timedelta(days=90)
_PURGE_INTERVAL_SECONDS = 3600.0


def _usage_columns(usage: Usage | None) -> tuple:
    if usage is None:
        return (None, None, None, None)
    extra = json.dumps(usage.extra) if usage.extra else None
    return (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens, extra)


def _call_from_row(row) -> Call:  # noqa: ANN001
    (
        id_,
        llm_name,
        operation,
        trace_id,
        status,
        kind,
        http_status,
        latency_ms,
        error_detail,
        prompt_tokens,
        completion_tokens,
        total_tokens,
        usage_extra,
        quality_score,
        call_id,
        called_at,
        scope,
        cooldown_until,
        key_hash_val,
    ) = row
    extra = json.loads(usage_extra) if usage_extra else None
    usage = None
    if any(v is not None for v in (prompt_tokens, completion_tokens, total_tokens, extra)):
        usage = Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            extra=extra,
        )
    return Call(
        id=str(id_),
        llm_name=str(llm_name),
        operation=operation,
        trace_id=trace_id,
        status=CallStatus(status) if status is not None else None,
        kind=str(kind),
        ts=datetime.fromisoformat(called_at) if called_at else None,
        http_status=http_status,
        latency_ms=latency_ms,
        error_detail=error_detail,
        usage=usage,
        quality_score=quality_score,
        call_id=call_id,
        scope=scope,
        cooldown_until=datetime.fromisoformat(cooldown_until) if cooldown_until else None,
        key_hash=key_hash_val,
    )


_SELECT_COLUMNS = (
    "id, llm_name, operation, trace_id, status, kind, http_status, latency_ms,"
    " error_detail, prompt_tokens, completion_tokens, total_tokens, usage_extra,"
    " quality_score, call_id, called_at, scope, cooldown_until, key_hash"
)


class Knowledge:
    """SQLite-backed queryable knowledge store over ``llmbroker_calls`` + the
    ``llmbroker_disabled`` admin verdict map. Self-purges call rows older than
    ``retention``, checked at most once per hour on write activity."""

    def __init__(self, db_path: str | Path, *, retention: timedelta = _DEFAULT_RETENTION) -> None:
        self._db_path = str(db_path)
        self._retention = retention
        self._last_purge = float("-inf")

    async def record(self, call: Call) -> None:
        pt, ct, tt, extra = _usage_columns(call.usage)
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            await db.execute(
                "INSERT INTO llmbroker_calls"
                " (id, llm_name, operation, trace_id, status, kind, http_status, latency_ms,"
                "  error_detail, prompt_tokens, completion_tokens, total_tokens, usage_extra,"
                "  quality_score, call_id, called_at, scope, cooldown_until, key_hash)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    call.id,
                    call.llm_name,
                    call.operation,
                    call.trace_id,
                    call.status.value if call.status is not None else None,
                    call.kind,
                    call.http_status,
                    call.latency_ms,
                    call.error_detail,
                    pt,
                    ct,
                    tt,
                    extra,
                    call.quality_score,
                    call.call_id,
                    (call.ts or datetime.now(UTC)).isoformat(),
                    call.scope,
                    call.cooldown_until.isoformat() if call.cooldown_until else None,
                    call.key_hash,
                ],
            )
            await db.commit()
        await self._maybe_purge()

    async def record_quality(
        self,
        llm_name: str,
        operation: str | None,
        score: float,
        *,
        call_id: str | None = None,
    ) -> None:
        """Append a self-contained quality record — never updates the call row."""
        await self.record(
            Call(
                id=str(uuid.uuid4()),
                llm_name=llm_name,
                operation=operation,
                trace_id=None,
                status=None,
                kind="quality",
                ts=datetime.now(UTC),
                quality_score=score,
                call_id=call_id,
            ),
        )

    async def calls(self, *, limit: int, scope: str | None = None) -> list[Call]:  # noqa: ARG002
        """Newest-first tail of the journal, both kinds interleaved — unfiltered by scope
        (learning is global); ``scope`` is accepted for the host-facing filter only."""
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            if scope is None:
                rows = await (
                    await db.execute(
                        f"SELECT {_SELECT_COLUMNS} FROM llmbroker_calls"  # noqa: S608
                        " ORDER BY called_at DESC LIMIT ?",
                        [limit],
                    )
                ).fetchall()
            else:
                rows = await (
                    await db.execute(
                        f"SELECT {_SELECT_COLUMNS} FROM llmbroker_calls"  # noqa: S608
                        " WHERE scope = ? ORDER BY called_at DESC LIMIT ?",
                        [scope, limit],
                    )
                ).fetchall()
        return [_call_from_row(r) for r in rows]

    async def _purge_old_calls(self) -> None:
        cutoff = datetime.now(UTC) - self._retention
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            await db.execute(
                "DELETE FROM llmbroker_calls WHERE called_at < ?",
                [cutoff.isoformat()],
            )
            await db.commit()

    async def _maybe_purge(self) -> None:
        now = time.monotonic()
        if now - self._last_purge < _PURGE_INTERVAL_SECONDS:
            return
        self._last_purge = now
        await self._purge_old_calls()

    async def metrics(
        self,
        *,
        since: datetime | None = None,
        user_id: int | str | None = None,
    ) -> dict:
        """Unused by the broker (metrics are served from the rebuild's cached tail);
        kept for hosts that want a direct read."""
        conditions: list[str] = ["scope IS ?", "kind = 'call'"]
        params: list = [user_id]
        if since is not None:
            conditions.append("called_at >= ?")
            params.append(since.isoformat())
        where = " WHERE " + " AND ".join(conditions)
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            rows = await (
                await db.execute(
                    f"SELECT llm_name, COUNT(*), MAX(called_at) FROM llmbroker_calls{where}"  # noqa: S608
                    " GROUP BY llm_name",
                    params,
                )
            ).fetchall()
            result: dict[str, LLMMetrics] = {}
            for r in rows:
                name = str(r[0])
                inner_params: list = [name, user_id]
                inner_sql = (
                    "SELECT status FROM llmbroker_calls"  # noqa: S608
                    " WHERE llm_name = ? AND scope IS ? AND kind = 'call'"
                )
                if since is not None:
                    inner_sql += " AND called_at >= ?"
                    inner_params.append(since.isoformat())
                inner_sql += " ORDER BY called_at DESC LIMIT 1"
                last = await (await db.execute(inner_sql, inner_params)).fetchone()
                last_status = CallStatus(last[0]) if last and last[0] else None
                last_at = datetime.fromisoformat(r[2]) if r[2] else None
                result[name] = LLMMetrics(
                    call_count=int(r[1]),
                    last_status=last_status,
                    last_at=last_at,
                )
        return result

    # ------------------------------------------------------------------
    # Admin disabled-verdict map
    # ------------------------------------------------------------------

    async def get_disabled(self, name: str) -> bool:
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            row = await (
                await db.execute(
                    "SELECT disabled FROM llmbroker_disabled WHERE name = ?",
                    [name],
                )
            ).fetchone()
        return bool(row[0]) if row else False

    async def set_disabled(self, name: str, flag: bool) -> None:  # noqa: FBT001
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            await db.execute(
                "INSERT INTO llmbroker_disabled (name, disabled) VALUES (?, ?)"
                " ON CONFLICT(name) DO UPDATE SET disabled = excluded.disabled",
                [name, int(flag)],
            )
            await db.commit()

    async def seed_disabled(self, names: list[str]) -> None:
        """Insert-if-absent every name with ``disabled=False`` — never touches existing values."""
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            await db.executemany(
                "INSERT OR IGNORE INTO llmbroker_disabled (name, disabled) VALUES (?, 0)",
                [(name,) for name in names],
            )
            await db.commit()

    async def disabled_map(self) -> dict[str, bool]:
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            rows = await (
                await db.execute("SELECT name, disabled FROM llmbroker_disabled")
            ).fetchall()
        return {str(r[0]): bool(r[1]) for r in rows}

    async def aclose(self) -> None:
        return

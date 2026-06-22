"""SQLite-backed queryable telemetry over ``llmbroker_calls``."""

import json
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from llmbroker.models import Call, CallStatus, LLMMetrics, Usage, check_user_id
from llmbroker.sqlite.schema import ensure_schema


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
        http_status,
        latency_ms,
        error_detail,
        prompt_tokens,
        completion_tokens,
        total_tokens,
        usage_extra,
        quality_score,
        user_id,
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
        status=CallStatus(status),
        http_status=http_status,
        latency_ms=latency_ms,
        error_detail=error_detail,
        usage=usage,
        quality_score=quality_score,
        user_id=user_id,
    )


class Telemetry:
    """SQLite-backed queryable telemetry over ``llmbroker_calls``."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    async def record(self, call: Call) -> None:
        pt, ct, tt, extra = _usage_columns(call.usage)
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            await db.execute(
                "INSERT INTO llmbroker_calls"
                " (id, llm_name, operation, trace_id, status, http_status, latency_ms,"
                "  error_detail, prompt_tokens, completion_tokens, total_tokens, usage_extra,"
                "  quality_score, called_at, user_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    call.id,
                    call.llm_name,
                    call.operation,
                    call.trace_id,
                    call.status.value,
                    call.http_status,
                    call.latency_ms,
                    call.error_detail,
                    pt,
                    ct,
                    tt,
                    extra,
                    call.quality_score,
                    datetime.now(UTC).isoformat(),
                    call.user_id,
                ],
            )
            await db.commit()

    async def record_quality(self, call_id: str, score: float) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            cursor = await db.execute(
                "UPDATE llmbroker_calls SET quality_score = ? WHERE id = ?",
                [score, call_id],
            )
            if cursor.rowcount == 0:
                raise KeyError(call_id)
            await db.commit()

    async def metrics(
        self,
        *,
        since: datetime | None = None,
        user_id: int | str | None = None,
    ) -> dict[str, LLMMetrics]:
        check_user_id(user_id)
        conditions: list[str] = ["user_id IS ?"]
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
                    " WHERE llm_name = ? AND user_id IS ?"
                )
                if since is not None:
                    inner_sql += " AND called_at >= ?"
                    inner_params.append(since.isoformat())
                inner_sql += " ORDER BY called_at DESC LIMIT 1"
                last = await (await db.execute(inner_sql, inner_params)).fetchone()
                last_status = CallStatus(last[0]) if last else None
                last_at = datetime.fromisoformat(r[2]) if r[2] else None
                result[name] = LLMMetrics(
                    call_count=int(r[1]),
                    last_status=last_status,
                    last_at=last_at,
                )
        return result

    async def calls(
        self,
        *,
        limit: int,
        user_id: int | str | None = None,
    ) -> list[Call]:
        check_user_id(user_id)
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            rows = await (
                await db.execute(
                    "SELECT id, llm_name, operation, trace_id, status, http_status, latency_ms,"  # noqa: S608
                    " error_detail, prompt_tokens, completion_tokens, total_tokens, usage_extra,"
                    " quality_score, user_id FROM llmbroker_calls"
                    " WHERE user_id IS ? ORDER BY called_at DESC LIMIT ?",
                    [user_id, limit],
                )
            ).fetchall()
        return [_call_from_row(r) for r in rows]

    async def purge_calls(self, *, before: datetime) -> int:
        """Delete all calls older than *before*, across all users. Admin operation."""
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            cursor = await db.execute(
                "DELETE FROM llmbroker_calls WHERE called_at < ?",
                [before.isoformat()],
            )
            await db.commit()
            return cursor.rowcount

    async def aclose(self) -> None:
        return

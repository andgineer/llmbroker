"""Postgres-backed queryable telemetry over ``llmbroker_calls``."""

import json
from datetime import UTC, datetime

import asyncpg

from llmbroker.models import Call, CallStatus, LLMMetrics, Usage, check_user_id
from llmbroker.postgres.schema import ensure_schema, to_uid


def _usage_columns(usage: Usage | None) -> tuple:
    if usage is None:
        return (None, None, None, None)
    extra = json.dumps(usage.extra) if usage.extra else None
    return (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens, extra)


def _call_from_row(row: asyncpg.Record) -> Call:
    extra = json.loads(row["usage_extra"]) if row["usage_extra"] else None
    usage = None
    if any(
        v is not None
        for v in (row["prompt_tokens"], row["completion_tokens"], row["total_tokens"], extra)
    ):
        usage = Usage(
            prompt_tokens=row["prompt_tokens"],
            completion_tokens=row["completion_tokens"],
            total_tokens=row["total_tokens"],
            extra=extra,
        )
    return Call(
        id=str(row["id"]),
        llm_name=str(row["llm_name"]),
        operation=row["operation"],
        trace_id=row["trace_id"],
        status=CallStatus(row["status"]),
        http_status=row["http_status"],
        latency_ms=row["latency_ms"],
        error_detail=row["error_detail"],
        usage=usage,
        quality_score=row["quality_score"],
        user_id=row["user_id"],
    )


class Telemetry:
    """Postgres-backed queryable telemetry over ``llmbroker_calls``."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def record(self, call: Call) -> None:
        pt, ct, tt, extra = _usage_columns(call.usage)
        await ensure_schema(self._pool)
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO llmbroker_calls"
                " (id, llm_name, operation, trace_id, status, http_status, latency_ms,"
                "  error_detail, prompt_tokens, completion_tokens, total_tokens, usage_extra,"
                "  quality_score, called_at, user_id)"
                " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)",
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
                datetime.now(UTC),
                to_uid(call.user_id),
            )

    async def record_quality(self, call_id: str, score: float) -> None:
        await ensure_schema(self._pool)
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                "UPDATE llmbroker_calls SET quality_score=$1 WHERE id=$2",
                score,
                call_id,
            )
        if int(status.split()[-1]) == 0:
            raise KeyError(call_id)

    async def metrics(
        self,
        *,
        since: datetime | None = None,
        user_id: int | str | None = None,
    ) -> dict[str, LLMMetrics]:
        check_user_id(user_id)
        uid = to_uid(user_id)
        await ensure_schema(self._pool)
        conditions = ["user_id IS NOT DISTINCT FROM $1"]
        params: list = [uid]
        if since is not None:
            params.append(since)
            conditions.append(f"called_at >= ${len(params)}")
        where = " WHERE " + " AND ".join(conditions)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT ON (llm_name) llm_name, status,"  # noqa: S608
                " COUNT(*) OVER (PARTITION BY llm_name) AS cnt,"
                " MAX(called_at) OVER (PARTITION BY llm_name) AS last_at"
                f" FROM llmbroker_calls{where}"
                " ORDER BY llm_name, called_at DESC",
                *params,
            )
        return {
            str(row["llm_name"]): LLMMetrics(
                call_count=int(row["cnt"]),
                last_status=CallStatus(row["status"]),
                last_at=row["last_at"],
            )
            for row in rows
        }

    async def calls(self, *, limit: int, user_id: int | str | None = None) -> list[Call]:
        check_user_id(user_id)
        uid = to_uid(user_id)
        await ensure_schema(self._pool)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, llm_name, operation, trace_id, status, http_status, latency_ms,"  # noqa: S608
                " error_detail, prompt_tokens, completion_tokens, total_tokens, usage_extra,"
                " quality_score, user_id FROM llmbroker_calls"
                " WHERE user_id IS NOT DISTINCT FROM $1 ORDER BY called_at DESC LIMIT $2",
                uid,
                limit,
            )
        return [_call_from_row(r) for r in rows]

    async def purge_calls(self, *, before: datetime) -> int:
        """Delete all calls older than *before*, across all users. Admin operation."""
        await ensure_schema(self._pool)
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                "DELETE FROM llmbroker_calls WHERE called_at < $1",
                before,
            )
        return int(status.split()[-1])

    async def aclose(self) -> None:
        return

"""Postgres-backed queryable knowledge store over ``llmbroker_calls`` + the
``llmbroker_disabled`` admin verdict map."""

import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg

from llmbroker.models import Call, CallStatus, LLMMetrics, Usage
from llmbroker.postgres.schema import ensure_schema

_DEFAULT_RETENTION = timedelta(days=90)
_PURGE_INTERVAL_SECONDS = 3600.0


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
    status = row["status"]
    return Call(
        id=str(row["id"]),
        llm_name=str(row["llm_name"]),
        operation=row["operation"],
        trace_id=row["trace_id"],
        status=CallStatus(status) if status is not None else None,
        kind=str(row["kind"]),
        ts=row["called_at"],
        http_status=row["http_status"],
        latency_ms=row["latency_ms"],
        error_detail=row["error_detail"],
        usage=usage,
        quality_score=row["quality_score"],
        call_id=row["call_id"],
        scope=row["scope"],
        cooldown_until=row["cooldown_until"],
        key_hash=row["key_hash"],
    )


class Knowledge:
    """Postgres-backed queryable knowledge store over ``llmbroker_calls`` + admin
    verdicts. Self-purges call rows older than ``retention``, checked at most
    once per hour on write activity."""

    def __init__(self, pool: asyncpg.Pool, *, retention: timedelta = _DEFAULT_RETENTION) -> None:
        self._pool = pool
        self._retention = retention
        self._last_purge = float("-inf")

    async def record(self, call: Call) -> None:
        pt, ct, tt, extra = _usage_columns(call.usage)
        await ensure_schema(self._pool)
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO llmbroker_calls"
                " (id, llm_name, operation, trace_id, status, kind, http_status, latency_ms,"
                "  error_detail, prompt_tokens, completion_tokens, total_tokens, usage_extra,"
                "  quality_score, call_id, called_at, scope, cooldown_until, key_hash)"
                " VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)",
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
                call.ts or datetime.now(UTC),
                call.scope,
                call.cooldown_until,
                call.key_hash,
            )
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

    async def metrics(
        self,
        *,
        since: datetime | None = None,
        user_id: int | str | None = None,
    ) -> dict[str, LLMMetrics]:
        """Unused by the broker (metrics are served from the rebuild's cached tail);
        kept for hosts that want a direct read."""
        await ensure_schema(self._pool)
        conditions = ["scope IS NOT DISTINCT FROM $1", "kind = 'call'"]
        params: list = [user_id]
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
                last_status=CallStatus(row["status"]) if row["status"] else None,
                last_at=row["last_at"],
            )
            for row in rows
        }

    async def calls(self, *, limit: int, scope: str | None = None) -> list[Call]:
        """Newest-first tail of the journal, both kinds interleaved — unfiltered by scope
        (learning is global); ``scope`` is accepted for the host-facing filter only."""
        await ensure_schema(self._pool)
        columns = (
            "id, llm_name, operation, trace_id, status, kind, http_status, latency_ms,"
            " error_detail, prompt_tokens, completion_tokens, total_tokens, usage_extra,"
            " quality_score, call_id, called_at, scope, cooldown_until, key_hash"
        )
        async with self._pool.acquire() as conn:
            if scope is None:
                rows = await conn.fetch(
                    f"SELECT {columns} FROM llmbroker_calls"  # noqa: S608
                    " ORDER BY called_at DESC LIMIT $1",
                    limit,
                )
            else:
                rows = await conn.fetch(
                    f"SELECT {columns} FROM llmbroker_calls"  # noqa: S608
                    " WHERE scope = $1 ORDER BY called_at DESC LIMIT $2",
                    scope,
                    limit,
                )
        return [_call_from_row(r) for r in rows]

    async def _purge_old_calls(self) -> None:
        cutoff = datetime.now(UTC) - self._retention
        await ensure_schema(self._pool)
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM llmbroker_calls WHERE called_at < $1",
                cutoff,
            )

    async def _maybe_purge(self) -> None:
        now = time.monotonic()
        if now - self._last_purge < _PURGE_INTERVAL_SECONDS:
            return
        self._last_purge = now
        await self._purge_old_calls()

    # ------------------------------------------------------------------
    # Admin disabled-verdict map
    # ------------------------------------------------------------------

    async def get_disabled(self, name: str) -> bool:
        await ensure_schema(self._pool)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT disabled FROM llmbroker_disabled WHERE name = $1",
                name,
            )
        return bool(row["disabled"]) if row else False

    async def set_disabled(self, name: str, flag: bool) -> None:  # noqa: FBT001
        await ensure_schema(self._pool)
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO llmbroker_disabled (name, disabled) VALUES ($1, $2)"
                " ON CONFLICT (name) DO UPDATE SET disabled = EXCLUDED.disabled",
                name,
                flag,
            )

    async def seed_disabled(self, names: list[str]) -> None:
        """Insert-if-absent every name with ``disabled=False`` — never touches existing values."""
        await ensure_schema(self._pool)
        async with self._pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO llmbroker_disabled (name, disabled) VALUES ($1, FALSE)"
                " ON CONFLICT (name) DO NOTHING",
                [(name,) for name in names],
            )

    async def disabled_map(self) -> dict[str, bool]:
        await ensure_schema(self._pool)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch("SELECT name, disabled FROM llmbroker_disabled")
        return {str(r["name"]): bool(r["disabled"]) for r in rows}

    async def aclose(self) -> None:
        return

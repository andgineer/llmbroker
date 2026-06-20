"""SQLite batteries: Registry (config), Telemetry (call journal), Secrets.

Each method opens a short-lived ``aiosqlite`` connection and ensures the schema.
All tables are ``llmbroker_``-prefixed and owned by ``ensure_schema``.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from llmbroker.models import Call, CallStatus, LLMConfig, LLMMetrics, Usage
from llmbroker.schema import ensure_schema


class Registry:
    """SQLite-backed mutable registry over ``llmbroker_registry``."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    async def load(self) -> list[LLMConfig]:
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db)
            rows = await (
                await db.execute(
                    "SELECT name, base_url, model, api_key_ref FROM llmbroker_registry"
                    " ORDER BY name",
                )
            ).fetchall()
        return [
            LLMConfig(name=str(r[0]), base_url=str(r[1]), model=str(r[2]), api_key_ref=str(r[3]))
            for r in rows
        ]

    async def get(self, name: str) -> LLMConfig | None:
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT name, base_url, model, api_key_ref FROM llmbroker_registry"
                    " WHERE name = ?",
                    [name],
                )
            ).fetchone()
        if row is None:
            return None
        return LLMConfig(
            name=str(row[0]),
            base_url=str(row[1]),
            model=str(row[2]),
            api_key_ref=str(row[3]),
        )

    async def add(self, cfg: LLMConfig) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db)
            await db.execute(
                "INSERT INTO llmbroker_registry (name, base_url, model, api_key_ref)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET"
                " base_url = excluded.base_url, model = excluded.model,"
                " api_key_ref = excluded.api_key_ref",
                [cfg.name, cfg.base_url, cfg.model, cfg.api_key_ref],
            )
            await db.commit()

    async def update(self, cfg: LLMConfig) -> None:
        await self.add(cfg)

    async def remove(self, name: str) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db)
            await db.execute("DELETE FROM llmbroker_registry WHERE name = ?", [name])
            await db.commit()

    async def aclose(self) -> None:
        return


def _usage_columns(usage: Usage | None) -> tuple:
    if usage is None:
        return (None, None, None, None)
    extra = json.dumps(usage.extra) if usage.extra else None
    return (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens, extra)


def _call_from_row(row) -> Call:  # noqa: ANN001
    extra = json.loads(row[11]) if row[11] else None
    usage = None
    if any(v is not None for v in (row[8], row[9], row[10], extra)):
        usage = Usage(
            prompt_tokens=row[8],
            completion_tokens=row[9],
            total_tokens=row[10],
            extra=extra,
        )
    return Call(
        id=str(row[0]),
        llm_name=str(row[1]),
        operation=row[2],
        trace_id=row[3],
        status=CallStatus(row[4]),
        http_status=row[5],
        latency_ms=row[6],
        error_detail=row[7],
        usage=usage,
        quality_score=row[12],
    )


class Telemetry:
    """SQLite-backed queryable telemetry over ``llmbroker_calls``."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    async def record(self, call: Call) -> None:
        pt, ct, tt, extra = _usage_columns(call.usage)
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db)
            await db.execute(
                "INSERT INTO llmbroker_calls"
                " (id, llm_name, operation, trace_id, status, http_status, latency_ms,"
                "  error_detail, prompt_tokens, completion_tokens, total_tokens, usage_extra,"
                "  quality_score, called_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                ],
            )
            await db.commit()

    async def record_quality(self, call_id: str, score: float) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db)
            await db.execute(
                "UPDATE llmbroker_calls SET quality_score = ? WHERE id = ?",
                [score, call_id],
            )
            await db.commit()

    async def metrics(self, *, since: datetime | None = None) -> dict[str, LLMMetrics]:
        where = ""
        params: list = []
        if since is not None:
            where = " WHERE called_at >= ?"
            params.append(since.isoformat())
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db)
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
                last = await (
                    await db.execute(
                        "SELECT status FROM llmbroker_calls WHERE llm_name = ?"
                        " ORDER BY called_at DESC LIMIT 1",
                        [name],
                    )
                ).fetchone()
                last_status = CallStatus(last[0]) if last else None
                last_at = datetime.fromisoformat(r[2]) if r[2] else None
                result[name] = LLMMetrics(
                    call_count=int(r[1]),
                    last_status=last_status,
                    last_at=last_at,
                )
        return result

    async def calls(self, *, limit: int) -> list[Call]:
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db)
            rows = await (
                await db.execute(
                    "SELECT id, llm_name, operation, trace_id, status, http_status, latency_ms,"
                    " error_detail, prompt_tokens, completion_tokens, total_tokens, usage_extra,"
                    " quality_score FROM llmbroker_calls ORDER BY called_at DESC LIMIT ?",
                    [limit],
                )
            ).fetchall()
        return [_call_from_row(r) for r in rows]

    async def purge_calls(self, *, before: datetime) -> int:
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db)
            cursor = await db.execute(
                "DELETE FROM llmbroker_calls WHERE called_at < ?",
                [before.isoformat()],
            )
            await db.commit()
            return cursor.rowcount

    async def aclose(self) -> None:
        return


class Secrets:
    """SQLite-backed mutable secrets store over ``llmbroker_secrets``."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    async def resolve(self, ref: str) -> str:
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db)
            row = await (
                await db.execute(
                    "SELECT value FROM llmbroker_secrets WHERE ref = ?",
                    [ref],
                )
            ).fetchone()
        if row is None:
            raise KeyError(f"sqlite.Secrets: ref {ref!r} not found")
        return str(row[0])

    async def set(self, ref: str, value: str) -> None:
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db)
            await db.execute(
                "INSERT INTO llmbroker_secrets (ref, value) VALUES (?, ?)"
                " ON CONFLICT(ref) DO UPDATE SET value = excluded.value",
                [ref, value],
            )
            await db.commit()

    async def aclose(self) -> None:
        return

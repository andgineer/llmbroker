"""SQLite batteries: Registry (config), Telemetry (call journal), Secrets.

Each method opens a short-lived ``aiosqlite`` connection and ensures the schema.
All tables are ``llmbroker_``-prefixed and owned by ``ensure_schema``.
"""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from llmbroker.models import (
    Call,
    CallStatus,
    LifecyclePhase,
    LLMConfig,
    LLMMetrics,
    LLMState,
    Usage,
)
from llmbroker.schema import ensure_schema
from llmbroker.secrets import UserScopeError


def _check_user_id(user_id: int | str | None) -> None:
    if user_id == "":
        raise ValueError("user_id must not be empty string; use None for unscoped")


class Registry:
    """SQLite-backed mutable registry over ``llmbroker_registry``."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    async def load(self, user_id: int | str | None = None) -> list[LLMConfig]:
        _check_user_id(user_id)
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            rows = await (
                await db.execute(
                    "SELECT name, base_url, model, api_key_ref FROM llmbroker_registry"
                    " WHERE user_id IS ? ORDER BY name",
                    [user_id],
                )
            ).fetchall()
        return [
            LLMConfig(name=str(r[0]), base_url=str(r[1]), model=str(r[2]), api_key_ref=str(r[3]))
            for r in rows
        ]

    async def get(
        self,
        name: str,
        user_id: int | str | None = None,
    ) -> LLMConfig | None:
        _check_user_id(user_id)
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            row = await (
                await db.execute(
                    "SELECT name, base_url, model, api_key_ref FROM llmbroker_registry"
                    " WHERE name = ? AND user_id IS ?",
                    [name, user_id],
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

    async def add(self, cfg: LLMConfig, user_id: int | str | None = None) -> None:
        _check_user_id(user_id)
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            try:
                await db.execute(
                    "INSERT INTO llmbroker_registry (name, base_url, model, api_key_ref, user_id)"
                    " VALUES (?, ?, ?, ?, ?)",
                    [cfg.name, cfg.base_url, cfg.model, cfg.api_key_ref, user_id],
                )
            except sqlite3.IntegrityError:
                raise ValueError(f"LLM {cfg.name!r} already exists") from None
            await db.commit()

    async def update(self, cfg: LLMConfig, user_id: int | str | None = None) -> None:
        _check_user_id(user_id)
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            cursor = await db.execute(
                "UPDATE llmbroker_registry SET base_url=?, model=?, api_key_ref=?"
                " WHERE name=? AND user_id IS ?",
                [cfg.base_url, cfg.model, cfg.api_key_ref, cfg.name, user_id],
            )
            if cursor.rowcount == 0:
                raise KeyError(cfg.name)
            await db.commit()

    async def remove(self, name: str, user_id: int | str | None = None) -> None:
        _check_user_id(user_id)
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            cursor = await db.execute(
                "DELETE FROM llmbroker_registry WHERE name = ? AND user_id IS ?",
                [name, user_id],
            )
            if cursor.rowcount == 0:
                raise KeyError(name)
            await db.commit()

    async def aclose(self) -> None:
        return


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
        _check_user_id(user_id)
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
        _check_user_id(user_id)
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


_TRUST_STORED_PHASES = frozenset({LifecyclePhase.OFFLINE, LifecyclePhase.PROBING})


class StateStore:
    """SQLite-backed state store over ``llmbroker_state``.

    Useful for any stateless server (multiple workers, restarts) that must
    preserve cooldown state between requests on a single machine. Not
    a cross-node store — use a redis/postgres backend for clusters.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    async def read(self, user_id: int | str | None = None) -> dict[str, LLMState]:
        _check_user_id(user_id)
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            rows = await (
                await db.execute(
                    "SELECT llm_name, phase, cooldown_until, fail_count"
                    " FROM llmbroker_state WHERE user_id IS ?",
                    [user_id],
                )
            ).fetchall()
        result: dict[str, LLMState] = {}
        now = datetime.now(UTC)
        for row in rows:
            name = str(row[0])
            stored_phase = LifecyclePhase(row[1])
            cooldown_until = datetime.fromisoformat(row[2]) if row[2] else None
            fail_count = int(row[3])
            if stored_phase in _TRUST_STORED_PHASES:
                phase = stored_phase
                if cooldown_until is not None and cooldown_until <= now:
                    cooldown_until = None
            elif cooldown_until is not None and cooldown_until > now:
                phase = LifecyclePhase.COOLING
            elif stored_phase in {LifecyclePhase.AVAILABLE, LifecyclePhase.COOLING}:
                phase = LifecyclePhase.AVAILABLE
                cooldown_until = None
            else:
                raise ValueError(
                    f"Unexpected stored phase {stored_phase!r}: "
                    "add it to _TRUST_STORED_PHASES or handle it explicitly",
                )
            result[name] = LLMState(
                phase=phase,
                cooldown_until=cooldown_until,
                fail_count=fail_count,
            )
        return result

    async def write(self, name: str, state: LLMState, user_id: int | str | None = None) -> None:
        _check_user_id(user_id)
        if state.cooldown_until is not None and state.cooldown_until.tzinfo is None:
            raise ValueError("LLMState.cooldown_until must be timezone-aware")
        cooldown_iso = (
            state.cooldown_until.isoformat()
            if state.cooldown_until is not None and state.phase is not LifecyclePhase.AVAILABLE
            else None
        )
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            await db.execute(
                "INSERT OR REPLACE INTO llmbroker_state"
                " (llm_name, phase, cooldown_until, fail_count, user_id)"
                " VALUES (?, ?, ?, ?, ?)",
                [name, state.phase.value, cooldown_iso, state.fail_count, user_id],
            )
            await db.commit()

    async def aclose(self) -> None:
        return


class Secrets:
    """SQLite-backed mutable secrets store over ``llmbroker_secrets``."""

    def __init__(self, db_path: str | Path, *, require_user_id: bool = False) -> None:
        self._db_path = str(db_path)
        self._require_user_id = require_user_id

    async def resolve(self, ref: str, user_id: int | str | None = None) -> str:
        _check_user_id(user_id)
        if self._require_user_id and user_id is None:
            raise UserScopeError(
                "sqlite.Secrets: user_id is required (require_user_id=True) but received None",
            )
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            row = await (
                await db.execute(
                    "SELECT value FROM llmbroker_secrets WHERE ref = ? AND user_id IS ?",
                    [ref, user_id],
                )
            ).fetchone()
        if row is None:
            raise KeyError(f"sqlite.Secrets: ref {ref!r} not found")
        return str(row[0])

    async def set(self, ref: str, value: str, user_id: int | str | None = None) -> None:
        _check_user_id(user_id)
        if self._require_user_id and user_id is None:
            raise UserScopeError(
                "sqlite.Secrets: user_id is required (require_user_id=True) but received None",
            )
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            await db.execute(
                "INSERT OR REPLACE INTO llmbroker_secrets (ref, value, user_id) VALUES (?, ?, ?)",
                [ref, value, user_id],
            )
            await db.commit()

    async def aclose(self) -> None:
        return

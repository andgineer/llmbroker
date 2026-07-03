"""Postgres-backed state store over ``llmbroker_state`` and ``llmbroker_summaries``."""

import json
from dataclasses import replace
from datetime import UTC, datetime

import asyncpg

from llmbroker.models import LifecyclePhase, LLMState, QualitySummary, check_user_id, reconcile
from llmbroker.postgres.schema import ensure_schema, to_uid

_UPSERT_SUMMARY_DELTA = """
INSERT INTO llmbroker_summaries
    (name, operation, kind, weight, weighted_good, weight_sq, count, user_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
ON CONFLICT (name, COALESCE(operation, ''), kind, COALESCE(user_id, ''))
DO UPDATE SET
    weight = llmbroker_summaries.weight * $9 + EXCLUDED.weight,
    weighted_good = llmbroker_summaries.weighted_good * $9 + EXCLUDED.weighted_good,
    weight_sq = llmbroker_summaries.weight_sq * $10 + EXCLUDED.weight_sq,
    count = llmbroker_summaries.count + EXCLUDED.count
"""

_INSERT_SUMMARY_IF_ABSENT = """
INSERT INTO llmbroker_summaries
    (name, operation, kind, weight, weighted_good, weight_sq, count, user_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
ON CONFLICT (name, COALESCE(operation, ''), kind, COALESCE(user_id, '')) DO NOTHING
"""


class StateStore:
    """Postgres-backed state store over ``llmbroker_state``."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def read(self, user_id: int | str | None = None) -> dict[str, LLMState]:
        check_user_id(user_id)
        uid = to_uid(user_id)
        await ensure_schema(self._pool)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT llm_name, state FROM llmbroker_state WHERE user_id IS NOT DISTINCT FROM $1",
                uid,
            )
        now = datetime.now(UTC)
        return {
            str(row["llm_name"]): reconcile(LLMState.from_dict(json.loads(row["state"])), now)
            for row in rows
        }

    async def write(self, name: str, state: LLMState, user_id: int | str | None = None) -> None:
        check_user_id(user_id)
        if state.cooldown_until is not None and state.cooldown_until.tzinfo is None:
            raise ValueError("LLMState.cooldown_until must be timezone-aware")
        uid = to_uid(user_id)
        cooldown_until = (
            state.cooldown_until
            if state.cooldown_until is not None and state.phase is not LifecyclePhase.AVAILABLE
            else None
        )
        payload = json.dumps(replace(state, cooldown_until=cooldown_until).to_dict())
        await ensure_schema(self._pool)
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO llmbroker_state (llm_name, state, user_id)"
                " VALUES ($1, $2::jsonb, $3)"
                " ON CONFLICT (llm_name, COALESCE(user_id, ''))"
                " DO UPDATE SET state=EXCLUDED.state",
                name,
                payload,
                uid,
            )

    async def apply_summary_delta(  # noqa: PLR0913
        self,
        name: str,
        operation: str | None,
        kind: str,
        decay_pow: float,
        add_weight: float,
        add_good: float,
        add_weight_sq: float,
        add_count: int,
        user_id: int | str | None = None,
    ) -> None:
        check_user_id(user_id)
        uid = to_uid(user_id)
        await ensure_schema(self._pool)
        async with self._pool.acquire() as conn:
            await conn.execute(
                _UPSERT_SUMMARY_DELTA,
                name,
                operation,
                kind,
                add_weight,
                add_good,
                add_weight_sq,
                add_count,
                uid,
                decay_pow,
                decay_pow * decay_pow,
            )

    async def read_summaries(
        self,
        user_id: int | str | None = None,
    ) -> dict[tuple[str, str | None, str], QualitySummary]:
        check_user_id(user_id)
        uid = to_uid(user_id)
        await ensure_schema(self._pool)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT name, operation, kind, weight, weighted_good, weight_sq, count"
                " FROM llmbroker_summaries WHERE user_id IS NOT DISTINCT FROM $1",
                uid,
            )
        return {
            (row["name"], row["operation"], row["kind"]): QualitySummary(
                weight=row["weight"],
                weighted_good=row["weighted_good"],
                weight_sq=row["weight_sq"],
                count=row["count"],
            )
            for row in rows
        }

    async def seed_summary(
        self,
        name: str,
        operation: str | None,
        kind: str,
        summary: QualitySummary,
        user_id: int | str | None = None,
    ) -> None:
        check_user_id(user_id)
        uid = to_uid(user_id)
        await ensure_schema(self._pool)
        async with self._pool.acquire() as conn:
            await conn.execute(
                _INSERT_SUMMARY_IF_ABSENT,
                name,
                operation,
                kind,
                summary.weight,
                summary.weighted_good,
                summary.weight_sq,
                summary.count,
                uid,
            )

    async def aclose(self) -> None:
        return

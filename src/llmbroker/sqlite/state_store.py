"""SQLite-backed state store over ``llmbroker_state`` and ``llmbroker_summaries``."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from llmbroker.models import LifecyclePhase, LLMState, QualitySummary, check_user_id, reconcile
from llmbroker.sqlite.schema import ensure_schema

_UPSERT_SUMMARY_DELTA = """
INSERT INTO llmbroker_summaries
    (name, operation, kind, weight, weighted_good, weight_sq, count, user_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(name, COALESCE(operation, ''), kind, COALESCE(user_id, ''))
DO UPDATE SET
    weight = llmbroker_summaries.weight * ? + excluded.weight,
    weighted_good = llmbroker_summaries.weighted_good * ? + excluded.weighted_good,
    weight_sq = llmbroker_summaries.weight_sq * ? + excluded.weight_sq,
    count = llmbroker_summaries.count + excluded.count
"""

_INSERT_SUMMARY_IF_ABSENT = """
INSERT INTO llmbroker_summaries
    (name, operation, kind, weight, weighted_good, weight_sq, count, user_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(name, COALESCE(operation, ''), kind, COALESCE(user_id, '')) DO NOTHING
"""


class StateStore:
    """SQLite-backed state store over ``llmbroker_state``.

    Useful for any stateless server (multiple workers, restarts) that must
    preserve cooldown state between requests on a single machine. Not
    a cross-node store — use a redis/postgres backend for clusters.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    async def read(self, user_id: int | str | None = None) -> dict[str, LLMState]:
        check_user_id(user_id)
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            rows = await (
                await db.execute(
                    "SELECT llm_name, state FROM llmbroker_state WHERE user_id IS ?",
                    [user_id],
                )
            ).fetchall()
        now = datetime.now(UTC)
        return {str(row[0]): reconcile(LLMState.from_dict(json.loads(row[1])), now) for row in rows}

    async def write(self, name: str, state: LLMState, user_id: int | str | None = None) -> None:
        check_user_id(user_id)
        if state.cooldown_until is not None and state.cooldown_until.tzinfo is None:
            raise ValueError("LLMState.cooldown_until must be timezone-aware")
        cooldown_until = (
            state.cooldown_until
            if state.cooldown_until is not None and state.phase is not LifecyclePhase.AVAILABLE
            else None
        )
        payload = json.dumps(replace(state, cooldown_until=cooldown_until).to_dict())
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            await db.execute(
                "INSERT OR REPLACE INTO llmbroker_state (llm_name, state, user_id)"
                " VALUES (?, ?, ?)",
                [name, payload, user_id],
            )
            await db.commit()

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
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            await db.execute(
                _UPSERT_SUMMARY_DELTA,
                [
                    name,
                    operation,
                    kind,
                    add_weight,
                    add_good,
                    add_weight_sq,
                    add_count,
                    user_id,
                    decay_pow,
                    decay_pow,
                    decay_pow * decay_pow,
                ],
            )
            await db.commit()

    async def read_summaries(
        self,
        user_id: int | str | None = None,
    ) -> dict[tuple[str, str | None, str], QualitySummary]:
        check_user_id(user_id)
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            rows = await (
                await db.execute(
                    "SELECT name, operation, kind, weight, weighted_good, weight_sq, count"
                    " FROM llmbroker_summaries WHERE user_id IS ?",
                    [user_id],
                )
            ).fetchall()
        return {
            (str(r[0]), r[1], str(r[2])): QualitySummary(
                weight=r[3],
                weighted_good=r[4],
                weight_sq=r[5],
                count=r[6],
            )
            for r in rows
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
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            await db.execute(
                _INSERT_SUMMARY_IF_ABSENT,
                [
                    name,
                    operation,
                    kind,
                    summary.weight,
                    summary.weighted_good,
                    summary.weight_sq,
                    summary.count,
                    user_id,
                ],
            )
            await db.commit()

    async def aclose(self) -> None:
        return

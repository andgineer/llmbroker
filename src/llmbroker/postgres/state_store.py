"""Postgres-backed state store over ``llmbroker_state``."""

import json
from dataclasses import replace
from datetime import UTC, datetime

import asyncpg

from llmbroker.models import LifecyclePhase, LLMState, check_user_id, reconcile
from llmbroker.postgres.schema import ensure_schema, to_uid


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

    async def aclose(self) -> None:
        return

"""Postgres-backed state store over ``llmbroker_state``."""

from datetime import UTC, datetime

import asyncpg

from llmbroker.models import LifecyclePhase, LLMState, check_user_id
from llmbroker.postgres.schema import ensure_schema, to_uid

_TRUST_STORED_PHASES = frozenset({LifecyclePhase.OFFLINE, LifecyclePhase.PROBING})


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
                "SELECT llm_name, phase, cooldown_until, fail_count FROM llmbroker_state"
                " WHERE user_id IS NOT DISTINCT FROM $1",
                uid,
            )
        result: dict[str, LLMState] = {}
        now = datetime.now(UTC)
        for row in rows:
            name = str(row["llm_name"])
            stored_phase = LifecyclePhase(row["phase"])
            cooldown_until: datetime | None = row["cooldown_until"]
            fail_count = int(row["fail_count"])
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
        check_user_id(user_id)
        if state.cooldown_until is not None and state.cooldown_until.tzinfo is None:
            raise ValueError("LLMState.cooldown_until must be timezone-aware")
        uid = to_uid(user_id)
        cooldown = (
            state.cooldown_until
            if state.cooldown_until is not None and state.phase is not LifecyclePhase.AVAILABLE
            else None
        )
        await ensure_schema(self._pool)
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO llmbroker_state (llm_name, phase, cooldown_until, fail_count, user_id)"
                " VALUES ($1, $2, $3, $4, $5)"
                " ON CONFLICT (llm_name, COALESCE(user_id, ''))"
                " DO UPDATE SET phase=EXCLUDED.phase, cooldown_until=EXCLUDED.cooldown_until,"
                "               fail_count=EXCLUDED.fail_count",
                name,
                state.phase.value,
                cooldown,
                state.fail_count,
                uid,
            )

    async def aclose(self) -> None:
        return

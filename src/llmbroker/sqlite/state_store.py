"""SQLite-backed state store over ``llmbroker_state``."""

from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from llmbroker.models import LifecyclePhase, LLMState, check_user_id
from llmbroker.sqlite.schema import ensure_schema

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
        check_user_id(user_id)
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
        check_user_id(user_id)
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

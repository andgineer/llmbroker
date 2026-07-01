"""SQLite-backed state store over ``llmbroker_state``."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from llmbroker.models import LifecyclePhase, LLMState, check_user_id, reconcile
from llmbroker.sqlite.schema import ensure_schema


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

    async def aclose(self) -> None:
        return

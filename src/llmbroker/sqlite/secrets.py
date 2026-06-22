"""SQLite-backed mutable secrets store over ``llmbroker_secrets``."""

from pathlib import Path

import aiosqlite

from llmbroker.exceptions import UserScopeError
from llmbroker.models import check_user_id
from llmbroker.sqlite.schema import ensure_schema


class Secrets:
    """SQLite-backed mutable secrets store over ``llmbroker_secrets``."""

    def __init__(self, db_path: str | Path, *, require_user_id: bool = False) -> None:
        self._db_path = str(db_path)
        self._require_user_id = require_user_id

    async def resolve(self, ref: str, user_id: int | str | None = None) -> str:
        check_user_id(user_id)
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
        check_user_id(user_id)
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

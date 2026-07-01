"""SQLite-backed mutable registry over ``llmbroker_registry``."""

import json
import sqlite3
from pathlib import Path

import aiosqlite

from llmbroker.models import LLMConfig, check_user_id
from llmbroker.sqlite.schema import ensure_schema


def _config_from_row(row: tuple) -> LLMConfig:
    metadata = json.loads(row[4]) if row[4] else {}
    return LLMConfig.from_metadata(
        name=str(row[0]),
        base_url=str(row[1]),
        model=str(row[2]),
        api_key_ref=str(row[3]),
        metadata=metadata,
    )


class Registry:
    """SQLite-backed mutable registry over ``llmbroker_registry``."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    async def load(self, user_id: int | str | None = None) -> list[LLMConfig]:
        check_user_id(user_id)
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            rows = await (
                await db.execute(
                    "SELECT name, base_url, model, api_key_ref, metadata FROM llmbroker_registry"
                    " WHERE user_id IS ? ORDER BY name",
                    [user_id],
                )
            ).fetchall()
        return [_config_from_row(r) for r in rows]

    async def get(
        self,
        name: str,
        user_id: int | str | None = None,
    ) -> LLMConfig | None:
        check_user_id(user_id)
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            row = await (
                await db.execute(
                    "SELECT name, base_url, model, api_key_ref, metadata FROM llmbroker_registry"
                    " WHERE name = ? AND user_id IS ?",
                    [name, user_id],
                )
            ).fetchone()
        if row is None:
            return None
        return _config_from_row(row)

    async def add(self, cfg: LLMConfig, user_id: int | str | None = None) -> None:
        check_user_id(user_id)
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            try:
                await db.execute(
                    "INSERT INTO llmbroker_registry"
                    " (name, base_url, model, api_key_ref, metadata, user_id)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        cfg.name,
                        cfg.base_url,
                        cfg.model,
                        cfg.api_key_ref,
                        json.dumps(cfg.to_metadata()),
                        user_id,
                    ],
                )
            except sqlite3.IntegrityError:
                raise ValueError(f"LLM {cfg.name!r} already exists") from None
            await db.commit()

    async def update(self, cfg: LLMConfig, user_id: int | str | None = None) -> None:
        check_user_id(user_id)
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            cursor = await db.execute(
                "UPDATE llmbroker_registry SET base_url=?, model=?, api_key_ref=?, metadata=?"
                " WHERE name=? AND user_id IS ?",
                [
                    cfg.base_url,
                    cfg.model,
                    cfg.api_key_ref,
                    json.dumps(cfg.to_metadata()),
                    cfg.name,
                    user_id,
                ],
            )
            if cursor.rowcount == 0:
                raise KeyError(cfg.name)
            await db.commit()

    async def remove(self, name: str, user_id: int | str | None = None) -> None:
        check_user_id(user_id)
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

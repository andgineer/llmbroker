"""SQLite-backed mutable registry over ``llmbroker_registry`` — a pure preset mirror."""

import json
from pathlib import Path

import aiosqlite

from llmbroker.models import LLMConfig, check_user_id
from llmbroker.sqlite.schema import ensure_schema


def _config_from_row(row) -> LLMConfig:  # noqa: ANN001
    metadata = json.loads(row[4]) if row[4] else {}
    return LLMConfig.from_metadata(
        name=str(row[0]),
        base_url=str(row[1]),
        model=str(row[2]),
        api_key_ref=str(row[3]),
        metadata=metadata,
    )


class Registry:
    """SQLite-backed mutable registry over ``llmbroker_registry`` — a pure preset mirror."""

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

    async def mirror(self, configs: list[LLMConfig], user_id: int | str | None = None) -> None:
        """Total mirror: add new, update existing, delete stored entries absent
        from ``configs`` — the only registry write path."""
        check_user_id(user_id)
        source_names = {c.name for c in configs}
        async with aiosqlite.connect(self._db_path) as db:
            await ensure_schema(db, self._db_path)
            existing_rows = await (
                await db.execute(
                    "SELECT name FROM llmbroker_registry WHERE user_id IS ?",
                    [user_id],
                )
            ).fetchall()
            existing_names = {str(r[0]) for r in existing_rows}

            for name in existing_names - source_names:
                await db.execute(
                    "DELETE FROM llmbroker_registry WHERE name = ? AND user_id IS ?",
                    [name, user_id],
                )
            for cfg in configs:
                metadata = json.dumps(cfg.to_metadata())
                if cfg.name in existing_names:
                    await db.execute(
                        "UPDATE llmbroker_registry SET base_url=?, model=?, api_key_ref=?,"
                        " metadata=? WHERE name=? AND user_id IS ?",
                        [cfg.base_url, cfg.model, cfg.api_key_ref, metadata, cfg.name, user_id],
                    )
                else:
                    await db.execute(
                        "INSERT INTO llmbroker_registry"
                        " (name, base_url, model, api_key_ref, metadata, user_id)"
                        " VALUES (?, ?, ?, ?, ?, ?)",
                        [cfg.name, cfg.base_url, cfg.model, cfg.api_key_ref, metadata, user_id],
                    )
            await db.commit()

    async def aclose(self) -> None:
        return

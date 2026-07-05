"""Postgres-backed mutable registry over ``llmbroker_registry`` — a pure preset mirror."""

import json

import asyncpg

from llmbroker.models import LLMConfig, check_user_id
from llmbroker.postgres.schema import ensure_schema, to_uid


def _config_from_row(row: asyncpg.Record) -> LLMConfig:
    metadata = json.loads(row["metadata"]) if row["metadata"] else {}
    return LLMConfig.from_metadata(
        name=row["name"],
        base_url=row["base_url"],
        model=row["model"],
        api_key_ref=row["api_key_ref"],
        metadata=metadata,
    )


class Registry:
    """Postgres-backed mutable registry over ``llmbroker_registry`` — a pure preset mirror."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def load(self, user_id: int | str | None = None) -> list[LLMConfig]:
        check_user_id(user_id)
        uid = to_uid(user_id)
        await ensure_schema(self._pool)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT name, base_url, model, api_key_ref, metadata FROM llmbroker_registry"
                " WHERE user_id IS NOT DISTINCT FROM $1 ORDER BY name",
                uid,
            )
        return [_config_from_row(r) for r in rows]

    async def mirror(self, configs: list[LLMConfig], user_id: int | str | None = None) -> None:
        """Total mirror: add new, update existing, delete stored entries absent
        from ``configs`` — the only registry write path."""
        check_user_id(user_id)
        uid = to_uid(user_id)
        source_names = {c.name for c in configs}
        await ensure_schema(self._pool)
        async with self._pool.acquire() as conn, conn.transaction():
            existing_rows = await conn.fetch(
                "SELECT name FROM llmbroker_registry WHERE user_id IS NOT DISTINCT FROM $1",
                uid,
            )
            existing_names = {r["name"] for r in existing_rows}

            for name in existing_names - source_names:
                await conn.execute(
                    "DELETE FROM llmbroker_registry"
                    " WHERE name=$1 AND user_id IS NOT DISTINCT FROM $2",
                    name,
                    uid,
                )
            for cfg in configs:
                metadata = json.dumps(cfg.to_metadata())
                if cfg.name in existing_names:
                    await conn.execute(
                        "UPDATE llmbroker_registry"
                        " SET base_url=$1, model=$2, api_key_ref=$3, metadata=$4::jsonb"
                        " WHERE name=$5 AND user_id IS NOT DISTINCT FROM $6",
                        cfg.base_url,
                        cfg.model,
                        cfg.api_key_ref,
                        metadata,
                        cfg.name,
                        uid,
                    )
                else:
                    await conn.execute(
                        "INSERT INTO llmbroker_registry"
                        " (name, base_url, model, api_key_ref, metadata, user_id)"
                        " VALUES ($1, $2, $3, $4, $5::jsonb, $6)",
                        cfg.name,
                        cfg.base_url,
                        cfg.model,
                        cfg.api_key_ref,
                        metadata,
                        uid,
                    )

    async def aclose(self) -> None:
        return

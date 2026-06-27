"""Postgres-backed mutable registry over ``llmbroker_registry``."""

import asyncpg

from llmbroker.models import LLMConfig, check_user_id
from llmbroker.postgres.schema import ensure_schema, to_uid


class Registry:
    """Postgres-backed mutable registry over ``llmbroker_registry``."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    async def load(self, user_id: int | str | None = None) -> list[LLMConfig]:
        check_user_id(user_id)
        uid = to_uid(user_id)
        await ensure_schema(self._pool)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT name, base_url, model, api_key_ref FROM llmbroker_registry"
                " WHERE user_id IS NOT DISTINCT FROM $1 ORDER BY name",
                uid,
            )
        return [
            LLMConfig(
                name=r["name"],
                base_url=r["base_url"],
                model=r["model"],
                api_key_ref=r["api_key_ref"],
            )
            for r in rows
        ]

    async def get(self, name: str, user_id: int | str | None = None) -> LLMConfig | None:
        check_user_id(user_id)
        uid = to_uid(user_id)
        await ensure_schema(self._pool)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT name, base_url, model, api_key_ref FROM llmbroker_registry"
                " WHERE name = $1 AND user_id IS NOT DISTINCT FROM $2",
                name,
                uid,
            )
        if row is None:
            return None
        return LLMConfig(
            name=row["name"],
            base_url=row["base_url"],
            model=row["model"],
            api_key_ref=row["api_key_ref"],
        )

    async def add(self, cfg: LLMConfig, user_id: int | str | None = None) -> None:
        check_user_id(user_id)
        uid = to_uid(user_id)
        await ensure_schema(self._pool)
        async with self._pool.acquire() as conn:
            try:
                await conn.execute(
                    "INSERT INTO llmbroker_registry (name, base_url, model, api_key_ref, user_id)"
                    " VALUES ($1, $2, $3, $4, $5)",
                    cfg.name,
                    cfg.base_url,
                    cfg.model,
                    cfg.api_key_ref,
                    uid,
                )
            except asyncpg.UniqueViolationError:
                raise ValueError(f"LLM {cfg.name!r} already exists") from None

    async def update(self, cfg: LLMConfig, user_id: int | str | None = None) -> None:
        check_user_id(user_id)
        uid = to_uid(user_id)
        await ensure_schema(self._pool)
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                "UPDATE llmbroker_registry SET base_url=$1, model=$2, api_key_ref=$3"
                " WHERE name=$4 AND user_id IS NOT DISTINCT FROM $5",
                cfg.base_url,
                cfg.model,
                cfg.api_key_ref,
                cfg.name,
                uid,
            )
        if int(status.split()[-1]) == 0:
            raise KeyError(cfg.name)

    async def remove(self, name: str, user_id: int | str | None = None) -> None:
        check_user_id(user_id)
        uid = to_uid(user_id)
        await ensure_schema(self._pool)
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                "DELETE FROM llmbroker_registry WHERE name=$1 AND user_id IS NOT DISTINCT FROM $2",
                name,
                uid,
            )
        if int(status.split()[-1]) == 0:
            raise KeyError(name)

    async def aclose(self) -> None:
        return

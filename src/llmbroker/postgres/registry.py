"""Postgres-backed mutable registry over ``llmbroker_registry``."""

import json

import asyncpg

from llmbroker.models import LLMConfig, LLMProfile, check_user_id
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
    """Postgres-backed mutable registry over ``llmbroker_registry``."""

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

    async def get(self, name: str, user_id: int | str | None = None) -> LLMConfig | None:
        check_user_id(user_id)
        uid = to_uid(user_id)
        await ensure_schema(self._pool)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT name, base_url, model, api_key_ref, metadata FROM llmbroker_registry"
                " WHERE name = $1 AND user_id IS NOT DISTINCT FROM $2",
                name,
                uid,
            )
        if row is None:
            return None
        return _config_from_row(row)

    async def add(self, cfg: LLMConfig, user_id: int | str | None = None) -> None:
        check_user_id(user_id)
        uid = to_uid(user_id)
        await ensure_schema(self._pool)
        async with self._pool.acquire() as conn:
            try:
                await conn.execute(
                    "INSERT INTO llmbroker_registry"
                    " (name, base_url, model, api_key_ref, metadata, user_id)"
                    " VALUES ($1, $2, $3, $4, $5::jsonb, $6)",
                    cfg.name,
                    cfg.base_url,
                    cfg.model,
                    cfg.api_key_ref,
                    json.dumps(cfg.to_metadata()),
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
                "UPDATE llmbroker_registry"
                " SET base_url=$1, model=$2, api_key_ref=$3, metadata=$4::jsonb"
                " WHERE name=$5 AND user_id IS NOT DISTINCT FROM $6",
                cfg.base_url,
                cfg.model,
                cfg.api_key_ref,
                json.dumps(cfg.to_metadata()),
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

    async def read_profiles(self, user_id: int | str | None = None) -> dict[str, LLMProfile]:
        check_user_id(user_id)
        uid = to_uid(user_id)
        await ensure_schema(self._pool)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT name, profile FROM llmbroker_registry"
                " WHERE user_id IS NOT DISTINCT FROM $1",
                uid,
            )
        return {
            row["name"]: LLMProfile.from_dict(json.loads(row["profile"]))
            if row["profile"]
            else LLMProfile()
            for row in rows
        }

    async def write_profile(
        self,
        name: str,
        profile: LLMProfile,
        user_id: int | str | None = None,
    ) -> None:
        check_user_id(user_id)
        uid = to_uid(user_id)
        await ensure_schema(self._pool)
        async with self._pool.acquire() as conn:
            status = await conn.execute(
                "UPDATE llmbroker_registry SET profile=$1::jsonb"
                " WHERE name=$2 AND user_id IS NOT DISTINCT FROM $3",
                json.dumps(profile.to_dict()),
                name,
                uid,
            )
        if int(status.split()[-1]) == 0:
            raise KeyError(name)

    async def aclose(self) -> None:
        return

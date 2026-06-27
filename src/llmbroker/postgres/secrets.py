"""Postgres-backed mutable secrets store over ``llmbroker_secrets``."""

import asyncpg

from llmbroker.exceptions import UserScopeError
from llmbroker.models import check_user_id
from llmbroker.postgres.schema import ensure_schema, to_uid


class Secrets:
    """Postgres-backed mutable secrets store over ``llmbroker_secrets``."""

    def __init__(self, pool: asyncpg.Pool, *, require_user_id: bool = False) -> None:
        self._pool = pool
        self._require_user_id = require_user_id

    async def resolve(self, ref: str, user_id: int | str | None = None) -> str:
        check_user_id(user_id)
        if self._require_user_id and user_id is None:
            raise UserScopeError(
                "postgres.Secrets: user_id is required (require_user_id=True) but received None",
            )
        uid = to_uid(user_id)
        await ensure_schema(self._pool)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM llmbroker_secrets"
                " WHERE ref=$1 AND user_id IS NOT DISTINCT FROM $2",
                ref,
                uid,
            )
        if row is None:
            raise KeyError(f"postgres.Secrets: ref {ref!r} not found")
        return str(row["value"])

    async def set(self, ref: str, value: str, user_id: int | str | None = None) -> None:
        check_user_id(user_id)
        if self._require_user_id and user_id is None:
            raise UserScopeError(
                "postgres.Secrets: user_id is required (require_user_id=True) but received None",
            )
        uid = to_uid(user_id)
        await ensure_schema(self._pool)
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO llmbroker_secrets (ref, value, user_id) VALUES ($1, $2, $3)"
                " ON CONFLICT (ref, COALESCE(user_id, '')) DO UPDATE SET value = EXCLUDED.value",
                ref,
                value,
                uid,
            )

    async def aclose(self) -> None:
        return

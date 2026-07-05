"""Index management for the mongodb backend.

``ensure_schema`` is the single authority for the package's mongodb indexes.
Every collection is ``llmbroker_``-prefixed. Idempotent by default in MongoDB.
Schema version tracked via ``llmbroker_schema_version``. One known
installation, upgraded manually — on a version-marker mismatch
``ensure_schema`` fails fast with an actionable error.
"""

import asyncio
from datetime import UTC, datetime

from motor.motor_asyncio import AsyncIOMotorDatabase

_SCHEMA_VERSION = 5
_schema_ready: set[int] = set()
_schema_lock = asyncio.Lock()


def ensure_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def ensure_schema(db: AsyncIOMotorDatabase) -> None:
    """Create the package's indexes if missing. Idempotent, version-aware."""
    if id(db) in _schema_ready:
        return
    async with _schema_lock:
        if id(db) in _schema_ready:
            return
        version_doc = await db["llmbroker_schema_version"].find_one({})
        current = int(version_doc["version"]) if version_doc else 0
        if current not in (0, _SCHEMA_VERSION):
            raise RuntimeError(
                f"llmbroker schema version {current} found, this release expects"
                f" {_SCHEMA_VERSION} — drop the llmbroker_* collections and restart"
                " (export registry/secrets/calls first if you need them)",
            )
        await db["llmbroker_registry"].create_index(
            [("name", 1), ("user_id", 1)],
            unique=True,
            name="llmbroker_registry_unique",
        )
        await db["llmbroker_calls"].create_index(
            [("llm_name", 1)],
            name="llmbroker_idx_calls_llm_name",
        )
        await db["llmbroker_calls"].create_index(
            [("called_at", -1)],
            name="llmbroker_idx_calls_called_at",
        )
        await db["llmbroker_disabled"].create_index(
            [("name", 1)],
            unique=True,
            name="llmbroker_disabled_unique",
        )
        await db["llmbroker_secrets"].create_index(
            [("ref", 1), ("user_id", 1)],
            unique=True,
            name="llmbroker_secrets_unique",
        )
        # Unused by the broker (shared cooldowns derive from the journal) but kept so
        # the standalone mongodb.StateStore class stays functional until it is deleted.
        await db["llmbroker_state"].create_index(
            [("llm_name", 1), ("user_id", 1)],
            unique=True,
            name="llmbroker_state_unique",
        )
        await db["llmbroker_summaries"].create_index(
            [("name", 1), ("operation", 1), ("kind", 1), ("user_id", 1)],
            unique=True,
            name="llmbroker_summaries_unique",
        )
        if current == 0:
            await db["llmbroker_schema_version"].replace_one(
                {},
                {"version": _SCHEMA_VERSION},
                upsert=True,
            )
        _schema_ready.add(id(db))

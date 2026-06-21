"""Version-aware schema management for the sqlite battery.

``ensure_schema`` is the single authority for the package's sqlite tables.
Every object is ``llmbroker_``-prefixed and owned by ``ensure_schema``.
Idempotent: safe to call repeatedly.
The schema version is tracked via ``PRAGMA user_version``. Later releases
hang additive, data-preserving ALTERs off the version marker.
"""

import aiosqlite

_SCHEMA_VERSION = 1

_CREATE_REGISTRY = """
CREATE TABLE IF NOT EXISTS llmbroker_registry (
    name        TEXT NOT NULL,
    base_url    TEXT NOT NULL,
    model       TEXT NOT NULL,
    api_key_ref TEXT NOT NULL,
    user_id     TEXT
)
"""

_CREATE_CALLS = """
CREATE TABLE IF NOT EXISTS llmbroker_calls (
    id                TEXT PRIMARY KEY,
    llm_name          TEXT NOT NULL,
    operation         TEXT,
    trace_id          TEXT,
    status            TEXT NOT NULL,
    http_status       INTEGER,
    latency_ms        INTEGER,
    error_detail      TEXT,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    total_tokens      INTEGER,
    usage_extra       TEXT,
    quality_score     REAL,
    called_at         TEXT NOT NULL,
    user_id           TEXT
)
"""

_CREATE_SECRETS = """
CREATE TABLE IF NOT EXISTS llmbroker_secrets (
    ref     TEXT NOT NULL,
    value   TEXT NOT NULL,
    user_id TEXT
)
"""

_CREATE_IDX_LLM_NAME = (
    "CREATE INDEX IF NOT EXISTS llmbroker_idx_calls_llm_name ON llmbroker_calls(llm_name)"
)

_CREATE_IDX_CALLED_AT = (
    "CREATE INDEX IF NOT EXISTS llmbroker_idx_calls_called_at ON llmbroker_calls(called_at)"
)

_CREATE_IDX_CALLS_USER_ID = (
    "CREATE INDEX IF NOT EXISTS llmbroker_idx_calls_user_id ON llmbroker_calls(user_id)"
)

_CREATE_IDX_REGISTRY_UNIQUE = (
    "CREATE UNIQUE INDEX IF NOT EXISTS llmbroker_registry_unique"
    " ON llmbroker_registry(name, COALESCE(user_id, ''))"
)

_CREATE_IDX_SECRETS_UNIQUE = (
    "CREATE UNIQUE INDEX IF NOT EXISTS llmbroker_secrets_unique"
    " ON llmbroker_secrets(ref, COALESCE(user_id, ''))"
)


async def ensure_schema(db: aiosqlite.Connection) -> None:
    """Create the package's tables/indexes if missing. Idempotent, version-aware."""
    await db.execute(_CREATE_REGISTRY)
    await db.execute(_CREATE_CALLS)
    await db.execute(_CREATE_SECRETS)
    await db.execute(_CREATE_IDX_LLM_NAME)
    await db.execute(_CREATE_IDX_CALLED_AT)
    await db.execute(_CREATE_IDX_CALLS_USER_ID)
    await db.execute(_CREATE_IDX_REGISTRY_UNIQUE)
    await db.execute(_CREATE_IDX_SECRETS_UNIQUE)
    cursor = await db.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    current = int(row[0]) if row else 0
    if current < _SCHEMA_VERSION:
        await db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
    await db.commit()

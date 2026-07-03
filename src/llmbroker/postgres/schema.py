"""Version-aware schema management for the postgres backend.

``ensure_schema`` is the single authority for the package's postgres tables.
Every object is ``llmbroker_``-prefixed. Idempotent: safe to call repeatedly.
The schema version is tracked via ``llmbroker_schema_version``.
"""

import asyncio

import asyncpg

_SCHEMA_VERSION = 4
_schema_ready: set[int] = set()
_schema_lock = asyncio.Lock()


def to_uid(user_id: int | str | None) -> str | None:
    return str(user_id) if user_id is not None else None


_CREATE_VERSION_TABLE = """\
CREATE TABLE IF NOT EXISTS llmbroker_schema_version (
    id      SMALLINT PRIMARY KEY DEFAULT 1,
    version INTEGER  NOT NULL,
    CHECK (id = 1)
)\
"""

# State/summaries are live caches rebuilt from traffic; no data migration needed.
_DROP_STATE = "DROP TABLE IF EXISTS llmbroker_state"
_DROP_SUMMARIES = "DROP TABLE IF EXISTS llmbroker_summaries"

_DDL = """\
CREATE TABLE IF NOT EXISTS llmbroker_registry (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    base_url    TEXT NOT NULL,
    model       TEXT NOT NULL,
    api_key_ref TEXT NOT NULL,
    user_id     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS llmbroker_registry_unique
    ON llmbroker_registry(name, COALESCE(user_id, ''));
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
    quality_score     DOUBLE PRECISION,
    called_at         TIMESTAMPTZ NOT NULL,
    user_id           TEXT
);
CREATE INDEX IF NOT EXISTS llmbroker_idx_calls_llm_name ON llmbroker_calls(llm_name);
CREATE INDEX IF NOT EXISTS llmbroker_idx_calls_called_at ON llmbroker_calls(called_at);
CREATE INDEX IF NOT EXISTS llmbroker_idx_calls_user_id  ON llmbroker_calls(user_id);
CREATE TABLE IF NOT EXISTS llmbroker_secrets (
    id      BIGSERIAL PRIMARY KEY,
    ref     TEXT NOT NULL,
    value   TEXT NOT NULL,
    user_id TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS llmbroker_secrets_unique
    ON llmbroker_secrets(ref, COALESCE(user_id, ''));
CREATE TABLE IF NOT EXISTS llmbroker_state (
    id       BIGSERIAL PRIMARY KEY,
    llm_name TEXT NOT NULL,
    state    JSONB NOT NULL,
    user_id  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS llmbroker_state_unique
    ON llmbroker_state(llm_name, COALESCE(user_id, ''));
CREATE TABLE IF NOT EXISTS llmbroker_summaries (
    id            BIGSERIAL PRIMARY KEY,
    name          TEXT NOT NULL,
    operation     TEXT,
    kind          TEXT NOT NULL,
    weight        DOUBLE PRECISION NOT NULL DEFAULT 0,
    weighted_good DOUBLE PRECISION NOT NULL DEFAULT 0,
    weight_sq     DOUBLE PRECISION NOT NULL DEFAULT 0,
    count         INTEGER NOT NULL DEFAULT 0,
    user_id       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS llmbroker_summaries_unique
    ON llmbroker_summaries(name, COALESCE(operation, ''), kind, COALESCE(user_id, ''));\
"""

_ADD_REGISTRY_METADATA = "ALTER TABLE llmbroker_registry ADD COLUMN IF NOT EXISTS metadata JSONB"

_ADD_REGISTRY_PROFILE = "ALTER TABLE llmbroker_registry ADD COLUMN IF NOT EXISTS profile JSONB"

_UPSERT_VERSION = """\
INSERT INTO llmbroker_schema_version (id, version)
VALUES (1, $1)
ON CONFLICT (id) DO UPDATE SET version = EXCLUDED.version\
"""


async def ensure_schema(pool: asyncpg.Pool) -> None:
    """Create the package's tables/indexes if missing. Idempotent, version-aware.

    Migrates inside one transaction; concurrent callers serialize on
    postgres's own ``ACCESS EXCLUSIVE`` lock for `CREATE`/`ALTER TABLE`, so
    (unlike sqlite) no separate cross-process lock is needed here.
    """
    if id(pool) in _schema_ready:
        return
    async with _schema_lock:
        if id(pool) in _schema_ready:
            return
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(_CREATE_VERSION_TABLE)
            row = await conn.fetchrow("SELECT version FROM llmbroker_schema_version WHERE id = 1")
            current = int(row["version"]) if row else 0
            if current < _SCHEMA_VERSION:
                await conn.execute(_DROP_STATE)
                await conn.execute(_DROP_SUMMARIES)
            await conn.execute(_DDL)
            if current < _SCHEMA_VERSION:
                await conn.execute(_ADD_REGISTRY_METADATA)
                await conn.execute(_ADD_REGISTRY_PROFILE)
                await conn.execute(_UPSERT_VERSION, _SCHEMA_VERSION)
        _schema_ready.add(id(pool))

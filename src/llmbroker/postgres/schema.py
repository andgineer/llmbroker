"""Version-aware schema management for the postgres backend.

``ensure_schema`` is the single authority for the package's postgres tables.
Every object is ``llmbroker_``-prefixed. Idempotent: safe to call repeatedly.
The schema version is tracked via ``llmbroker_schema_version``. One known
installation, upgraded manually — on a fresh database the schema is created
and stamped; on a version-marker mismatch ``ensure_schema`` fails fast with an
actionable error instead of attempting an in-place migration.
"""

import asyncio

import asyncpg

_SCHEMA_VERSION = 5
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

_DDL = """\
CREATE TABLE IF NOT EXISTS llmbroker_registry (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    base_url    TEXT NOT NULL,
    model       TEXT NOT NULL,
    api_key_ref TEXT NOT NULL,
    metadata    JSONB,
    user_id     TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS llmbroker_registry_unique
    ON llmbroker_registry(name, COALESCE(user_id, ''));
CREATE TABLE IF NOT EXISTS llmbroker_calls (
    id                TEXT PRIMARY KEY,
    llm_name          TEXT NOT NULL,
    operation         TEXT,
    trace_id          TEXT,
    status            TEXT,
    kind              TEXT NOT NULL DEFAULT 'call',
    http_status       INTEGER,
    latency_ms        INTEGER,
    error_detail      TEXT,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    total_tokens      INTEGER,
    usage_extra       TEXT,
    quality_score     DOUBLE PRECISION,
    call_id           TEXT,
    called_at         TIMESTAMPTZ NOT NULL,
    scope             TEXT,
    cooldown_until    TIMESTAMPTZ,
    key_hash          TEXT
);
CREATE INDEX IF NOT EXISTS llmbroker_idx_calls_llm_name ON llmbroker_calls(llm_name);
CREATE INDEX IF NOT EXISTS llmbroker_idx_calls_called_at ON llmbroker_calls(called_at);
CREATE TABLE IF NOT EXISTS llmbroker_disabled (
    name     TEXT PRIMARY KEY,
    disabled BOOLEAN NOT NULL DEFAULT FALSE
);
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
# llmbroker_state / llmbroker_summaries are unused by the broker (shared cooldowns
# derive from the journal) but kept so the standalone postgres.StateStore class stays
# functional until it is deleted outright.

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
            if current not in (0, _SCHEMA_VERSION):
                raise RuntimeError(
                    f"llmbroker schema version {current} found, this release expects"
                    f" {_SCHEMA_VERSION} — drop the llmbroker_* tables and restart"
                    " (export registry/secrets/calls first if you need them)",
                )
            await conn.execute(_DDL)
            if current == 0:
                await conn.execute(_UPSERT_VERSION, _SCHEMA_VERSION)
        _schema_ready.add(id(pool))

"""Version-aware schema management for the sqlite backend.

``ensure_schema`` is the single authority for the package's sqlite tables.
Every object is ``llmbroker_``-prefixed and owned by ``ensure_schema``.
Idempotent: safe to call repeatedly. One known installation, upgraded
manually by its operator — on a fresh database the schema is created and
stamped; on a version-marker mismatch ``ensure_schema`` fails fast with an
actionable error instead of attempting an in-place migration.
"""

import aiosqlite

_SCHEMA_VERSION = 5

_CREATE_REGISTRY = """
CREATE TABLE IF NOT EXISTS llmbroker_registry (
    name        TEXT NOT NULL,
    base_url    TEXT NOT NULL,
    model       TEXT NOT NULL,
    api_key_ref TEXT NOT NULL,
    metadata    TEXT,
    user_id     TEXT
)
"""

_CREATE_CALLS = """
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
    quality_score     REAL,
    call_id           TEXT,
    called_at         TEXT NOT NULL,
    scope             TEXT,
    cooldown_until    TEXT,
    key_hash          TEXT
)
"""

_CREATE_DISABLED = """
CREATE TABLE IF NOT EXISTS llmbroker_disabled (
    name     TEXT PRIMARY KEY,
    disabled INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_SECRETS = """
CREATE TABLE IF NOT EXISTS llmbroker_secrets (
    ref     TEXT NOT NULL,
    value   TEXT NOT NULL,
    user_id TEXT
)
"""

# Unused by the broker (shared cooldowns derive from the journal) but kept so the
# standalone sqlite.StateStore class stays functional until it is deleted outright.
_CREATE_STATE = """
CREATE TABLE IF NOT EXISTS llmbroker_state (
    llm_name TEXT NOT NULL,
    state    TEXT NOT NULL,
    user_id  TEXT
)
"""

_CREATE_IDX_STATE_UNIQUE = (
    "CREATE UNIQUE INDEX IF NOT EXISTS llmbroker_state_unique"
    " ON llmbroker_state(llm_name, COALESCE(user_id, ''))"
)

_CREATE_SUMMARIES = """
CREATE TABLE IF NOT EXISTS llmbroker_summaries (
    name          TEXT NOT NULL,
    operation     TEXT,
    kind          TEXT NOT NULL,
    weight        REAL NOT NULL DEFAULT 0,
    weighted_good REAL NOT NULL DEFAULT 0,
    weight_sq     REAL NOT NULL DEFAULT 0,
    count         INTEGER NOT NULL DEFAULT 0,
    user_id       TEXT
)
"""

_CREATE_IDX_SUMMARIES_UNIQUE = (
    "CREATE UNIQUE INDEX IF NOT EXISTS llmbroker_summaries_unique"
    " ON llmbroker_summaries(name, COALESCE(operation, ''), kind, COALESCE(user_id, ''))"
)

_CREATE_IDX_LLM_NAME = (
    "CREATE INDEX IF NOT EXISTS llmbroker_idx_calls_llm_name ON llmbroker_calls(llm_name)"
)

_CREATE_IDX_CALLED_AT = (
    "CREATE INDEX IF NOT EXISTS llmbroker_idx_calls_called_at ON llmbroker_calls(called_at)"
)

_CREATE_IDX_REGISTRY_UNIQUE = (
    "CREATE UNIQUE INDEX IF NOT EXISTS llmbroker_registry_unique"
    " ON llmbroker_registry(name, COALESCE(user_id, ''))"
)

_CREATE_IDX_SECRETS_UNIQUE = (
    "CREATE UNIQUE INDEX IF NOT EXISTS llmbroker_secrets_unique"
    " ON llmbroker_secrets(ref, COALESCE(user_id, ''))"
)

_schema_ready: dict[str, int] = {}


async def _apply_ddl(db: aiosqlite.Connection) -> None:
    """Create the schema from scratch on a fresh database; fail fast on a version
    mismatch — there is exactly one known installation, upgraded manually."""
    cursor = await db.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    current = int(row[0]) if row else 0
    if current not in (0, _SCHEMA_VERSION):
        raise RuntimeError(
            f"llmbroker schema version {current} found, this release expects"
            f" {_SCHEMA_VERSION} — drop the llmbroker_* tables and restart"
            " (export registry/secrets/calls first if you need them)",
        )
    await db.execute(_CREATE_REGISTRY)
    await db.execute(_CREATE_CALLS)
    await db.execute(_CREATE_DISABLED)
    await db.execute(_CREATE_SECRETS)
    await db.execute(_CREATE_STATE)
    await db.execute(_CREATE_SUMMARIES)
    await db.execute(_CREATE_IDX_LLM_NAME)
    await db.execute(_CREATE_IDX_CALLED_AT)
    await db.execute(_CREATE_IDX_REGISTRY_UNIQUE)
    await db.execute(_CREATE_IDX_SECRETS_UNIQUE)
    await db.execute(_CREATE_IDX_STATE_UNIQUE)
    await db.execute(_CREATE_IDX_SUMMARIES_UNIQUE)
    if current == 0:
        await db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")


async def ensure_schema(db: aiosqlite.Connection, db_path: str = "") -> None:
    """Create the package's tables/indexes if missing. Idempotent, version-aware.

    A real file path migrates under its own ``isolation_level=None`` connection
    and a ``BEGIN IMMEDIATE`` transaction, so concurrent OS processes serialize
    on sqlite's file lock instead of racing. A dedicated connection is required
    because aiosqlite connections are thread-bound: mutating ``isolation_level``
    on an already-open connection from the caller's thread raises
    ``sqlite3.ProgrammingError``. ``:memory:`` has no cross-process concern, so
    it runs the same migration directly against *db*.
    """
    if db_path and db_path != ":memory:":
        if _schema_ready.get(db_path) == _SCHEMA_VERSION:
            return
        async with aiosqlite.connect(db_path, isolation_level=None) as migration_db:
            await migration_db.execute("BEGIN IMMEDIATE")
            try:
                await _apply_ddl(migration_db)
                await migration_db.execute("COMMIT")
            except BaseException:
                await migration_db.execute("ROLLBACK")
                raise
        _schema_ready[db_path] = _SCHEMA_VERSION
    else:
        await _apply_ddl(db)
        await db.commit()

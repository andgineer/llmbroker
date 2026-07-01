"""Version-aware schema management for the sqlite backend.

``ensure_schema`` is the single authority for the package's sqlite tables.
Every object is ``llmbroker_``-prefixed and owned by ``ensure_schema``.
Idempotent: safe to call repeatedly.
The schema version is tracked via ``PRAGMA user_version``. Later releases
hang additive, data-preserving ALTERs off the version marker.
"""

import aiosqlite

_SCHEMA_VERSION = 2

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

_ADD_REGISTRY_METADATA = "ALTER TABLE llmbroker_registry ADD COLUMN metadata TEXT"

_schema_ready: dict[str, int] = {}


async def _apply_ddl(db: aiosqlite.Connection) -> None:
    """Run the version-gated migration phases against *db* (no transaction control)."""
    cursor = await db.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    current = int(row[0]) if row else 0
    if current < _SCHEMA_VERSION:
        # State is a live cache rebuilt from traffic; no data migration needed.
        await db.execute("DROP TABLE IF EXISTS llmbroker_state")
    await db.execute(_CREATE_REGISTRY)
    await db.execute(_CREATE_CALLS)
    await db.execute(_CREATE_SECRETS)
    await db.execute(_CREATE_STATE)
    await db.execute(_CREATE_IDX_LLM_NAME)
    await db.execute(_CREATE_IDX_CALLED_AT)
    await db.execute(_CREATE_IDX_CALLS_USER_ID)
    await db.execute(_CREATE_IDX_REGISTRY_UNIQUE)
    await db.execute(_CREATE_IDX_SECRETS_UNIQUE)
    await db.execute(_CREATE_IDX_STATE_UNIQUE)
    if current < _SCHEMA_VERSION:
        table_info = await (await db.execute("PRAGMA table_info(llmbroker_registry)")).fetchall()
        existing_cols = {r[1] for r in table_info}
        if "metadata" not in existing_cols:
            await db.execute(_ADD_REGISTRY_METADATA)
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

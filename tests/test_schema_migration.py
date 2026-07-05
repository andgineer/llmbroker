"""Schema policy: single known installation, create-fresh or fail-fast on mismatch.

No in-place migration exists — ``ensure_schema`` creates the current shape from
scratch on a fresh database, and raises a clear, actionable error on any other
stamped version.
"""

import aiosqlite
import pytest

from llmbroker.backends.spec import SCHEMA_VERSION
from llmbroker.mongodb.driver import MongoDriver
from llmbroker.postgres.driver import PostgresDriver
from llmbroker.sqlite.driver import SqliteDriver


async def test_sqlite_ensure_schema_creates_fresh_and_stamps_version(tmp_path):
    db_path = str(tmp_path / "fresh.db")
    await SqliteDriver(db_path).ensure_schema()

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        assert row[0] == SCHEMA_VERSION

        table_info = await (await db.execute("PRAGMA table_info(llmbroker_registry)")).fetchall()
        col_names = {c[1] for c in table_info}
        assert "metadata" in col_names

        calls_cols = {
            c[1] for c in await (await db.execute("PRAGMA table_info(llmbroker_calls)")).fetchall()
        }
        assert {"kind", "call_id", "scope", "cooldown_until", "key_hash"} <= calls_cols

        disabled_cols = {
            c[1]
            for c in await (await db.execute("PRAGMA table_info(llmbroker_disabled)")).fetchall()
        }
        assert {"name", "disabled"} <= disabled_cols


async def test_sqlite_ensure_schema_is_idempotent(tmp_path):
    db_path = str(tmp_path / "idempotent.db")
    await SqliteDriver(db_path).ensure_schema()
    await SqliteDriver(db_path).ensure_schema()  # must not raise


async def test_sqlite_ensure_schema_raises_on_version_mismatch(tmp_path):
    db_path = str(tmp_path / "stale.db")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA user_version = 1")
        await db.commit()

    with pytest.raises(RuntimeError, match="schema version 1"):
        await SqliteDriver(db_path).ensure_schema()


async def test_postgres_ensure_schema_creates_fresh_and_stamps_version(pg_pool):
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS llmbroker_schema_version")

    await PostgresDriver(pg_pool).ensure_schema()

    async with pg_pool.acquire() as conn:
        version_row = await conn.fetchrow(
            "SELECT version FROM llmbroker_schema_version WHERE id = 1",
        )
        assert version_row["version"] == SCHEMA_VERSION

        cols = await conn.fetch(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = 'llmbroker_calls'",
        )
        col_names = {r["column_name"] for r in cols}
        assert {"kind", "call_id", "scope", "cooldown_until", "key_hash"} <= col_names


async def test_postgres_ensure_schema_raises_on_version_mismatch(pg_pool):
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO llmbroker_schema_version (id, version) VALUES (1, 1)"
            " ON CONFLICT (id) DO UPDATE SET version = 1",
        )

    try:
        with pytest.raises(RuntimeError, match="schema version 1"):
            await PostgresDriver(pg_pool).ensure_schema()
    finally:
        # Restore a clean, current-version marker so later tests sharing this
        # session-scoped pool don't inherit the deliberately-broken state.
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO llmbroker_schema_version (id, version) VALUES (1, $1)"
                " ON CONFLICT (id) DO UPDATE SET version = $1",
                SCHEMA_VERSION,
            )


async def test_mongodb_ensure_schema_creates_fresh_and_stamps_version(mongo_db):
    await mongo_db["llmbroker_schema_version"].delete_many({})

    await MongoDriver(mongo_db).ensure_schema()

    version_doc = await mongo_db["llmbroker_schema_version"].find_one({})
    assert version_doc["version"] == SCHEMA_VERSION

    indexes = await mongo_db["llmbroker_registry"].index_information()
    assert "llmbroker_registry_unique" in indexes


async def test_mongodb_ensure_schema_raises_on_version_mismatch(mongo_db):
    await mongo_db["llmbroker_schema_version"].replace_one({}, {"version": 1}, upsert=True)

    try:
        with pytest.raises(RuntimeError, match="schema version 1"):
            await MongoDriver(mongo_db).ensure_schema()
    finally:
        # Restore a clean, current-version marker so later tests sharing this
        # session-scoped database don't inherit the deliberately-broken state.
        await mongo_db["llmbroker_schema_version"].replace_one(
            {},
            {"version": SCHEMA_VERSION},
            upsert=True,
        )

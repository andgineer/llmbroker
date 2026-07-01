"""Version-gated migration tests: pre-plan shape -> ``ensure_schema`` -> current shape."""

import aiosqlite

from llmbroker.postgres import schema as pg_schema
from llmbroker.sqlite import schema as sqlite_schema


async def test_sqlite_ensure_schema_migrates_old_state_and_registry_shape(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE llmbroker_registry (name TEXT NOT NULL, base_url TEXT NOT NULL,"
            " model TEXT NOT NULL, api_key_ref TEXT NOT NULL, user_id TEXT)",
        )
        await db.execute(
            "CREATE TABLE llmbroker_state (llm_name TEXT NOT NULL, phase TEXT NOT NULL,"
            " cooldown_until TEXT, fail_count INTEGER NOT NULL DEFAULT 0, user_id TEXT)",
        )
        await db.execute(
            "INSERT INTO llmbroker_registry (name, base_url, model, api_key_ref, user_id)"
            " VALUES ('legacy-llm', 'https://legacy/v1', 'm', 'K', NULL)",
        )
        await db.execute("PRAGMA user_version = 1")
        await db.commit()

    async with aiosqlite.connect(db_path) as db:
        await sqlite_schema.ensure_schema(db, db_path)

        cursor = await db.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        assert row[0] == sqlite_schema._SCHEMA_VERSION

        table_info = await (await db.execute("PRAGMA table_info(llmbroker_registry)")).fetchall()
        col_names = {c[1] for c in table_info}
        assert "metadata" in col_names

        row = await (
            await db.execute(
                "SELECT base_url, metadata FROM llmbroker_registry WHERE name = ?",
                ["legacy-llm"],
            )
        ).fetchone()
        assert row[0] == "https://legacy/v1"
        assert row[1] is None

        state_cols = {
            c[1] for c in await (await db.execute("PRAGMA table_info(llmbroker_state)")).fetchall()
        }
        assert {"llm_name", "state", "user_id"} <= state_cols
        assert "phase" not in state_cols


async def test_postgres_ensure_schema_migrates_old_state_and_registry_shape(pg_pool):
    async with pg_pool.acquire() as conn:
        await conn.execute("DROP TABLE IF EXISTS llmbroker_state")
        await conn.execute(
            "CREATE TABLE llmbroker_state (id BIGSERIAL PRIMARY KEY, llm_name TEXT NOT NULL,"
            " phase TEXT NOT NULL, cooldown_until TIMESTAMPTZ,"
            " fail_count INTEGER NOT NULL DEFAULT 0, user_id TEXT)",
        )
        await conn.execute("ALTER TABLE llmbroker_registry DROP COLUMN IF EXISTS metadata")
        await conn.execute(
            "INSERT INTO llmbroker_registry (name, base_url, model, api_key_ref, user_id)"
            " VALUES ('legacy-llm', 'https://legacy/v1', 'm', 'K', NULL)",
        )
        await conn.execute(
            "INSERT INTO llmbroker_schema_version (id, version) VALUES (1, 1)"
            " ON CONFLICT (id) DO UPDATE SET version = 1",
        )

    # Clear the fast-path cache so ensure_schema re-runs its migration check.
    pg_schema._schema_ready.discard(id(pg_pool))
    try:
        await pg_schema.ensure_schema(pg_pool)

        async with pg_pool.acquire() as conn:
            version_row = await conn.fetchrow(
                "SELECT version FROM llmbroker_schema_version WHERE id = 1",
            )
            assert version_row["version"] == pg_schema._SCHEMA_VERSION

            cols = await conn.fetch(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'llmbroker_registry'",
            )
            assert "metadata" in {r["column_name"] for r in cols}

            row = await conn.fetchrow(
                "SELECT base_url, metadata FROM llmbroker_registry WHERE name = 'legacy-llm'",
            )
            assert row["base_url"] == "https://legacy/v1"
            assert row["metadata"] is None

            state_cols = await conn.fetch(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'llmbroker_state'",
            )
            state_col_names = {r["column_name"] for r in state_cols}
            assert {"llm_name", "state", "user_id"} <= state_col_names
            assert "phase" not in state_col_names
    finally:
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM llmbroker_registry WHERE name = 'legacy-llm'")

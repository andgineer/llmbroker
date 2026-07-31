"""Schema policy: single known installation, create-fresh or fail-fast on mismatch.

No in-place migration exists — ``ensure_schema`` creates the current shape from
scratch on a fresh database, and raises a clear, actionable error on any other
stamped version.
"""

import aiosqlite
import pytest

from llmbroker.backends.spec import SCHEMA_VERSION
from llmbroker.exceptions import SchemaVersionError
from llmbroker.mongodb.driver import MongoDriver
from llmbroker.postgres.driver import PostgresDriver
from llmbroker.sqlite.driver import SqliteDriver, _schema_ready


async def _marker(db_path):
    async with aiosqlite.connect(db_path) as db:
        row = await (
            await db.execute("SELECT version FROM llmbroker_schema_version WHERE id = 1")
        ).fetchone()
    return row[0] if row else None


async def _header(db_path):
    async with aiosqlite.connect(db_path) as db:
        row = await (await db.execute("PRAGMA user_version")).fetchone()
    return row[0]


async def _ensure_schema(db_path):
    """The per-path memo makes a second ``ensure_schema`` a no-op; these tests
    deliberately reopen the same file after editing it behind the driver's back."""
    _schema_ready.pop(db_path, None)
    await SqliteDriver(db_path).ensure_schema()


async def _make_legacy_db(db_path, header_version):
    """A database as an older release left it: llmbroker tables, no marker table,
    the version in the file header."""
    await _ensure_schema(db_path)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DROP TABLE llmbroker_schema_version")
        await db.execute(f"PRAGMA user_version = {header_version}")
        await db.commit()


async def test_sqlite_ensure_schema_creates_fresh_and_stamps_version(tmp_path):
    db_path = str(tmp_path / "fresh.db")
    await SqliteDriver(db_path).ensure_schema()

    assert await _marker(db_path) == SCHEMA_VERSION
    assert await _header(db_path) == 0

    async with aiosqlite.connect(db_path) as db:
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
    await SqliteDriver(db_path).ensure_schema()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE llmbroker_schema_version SET version = 1 WHERE id = 1")
        await db.commit()

    with pytest.raises(SchemaVersionError, match="schema version 1") as excinfo:
        await _ensure_schema(db_path)
    assert (excinfo.value.found, excinfo.value.expected) == (1, SCHEMA_VERSION)


async def test_schema_version_error_is_still_a_runtime_error(tmp_path):
    """Hosts written against the untyped raise keep working — the property that
    makes the typed exception an additive change."""
    db_path = str(tmp_path / "stale-compat.db")
    await SqliteDriver(db_path).ensure_schema()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("UPDATE llmbroker_schema_version SET version = 1 WHERE id = 1")
        await db.commit()

    with pytest.raises(RuntimeError):
        await _ensure_schema(db_path)


async def test_sqlite_adopts_legacy_header_and_hands_it_back(tmp_path):
    db_path = str(tmp_path / "legacy-current.db")
    await _make_legacy_db(db_path, SCHEMA_VERSION)

    await _ensure_schema(db_path)

    assert await _marker(db_path) == SCHEMA_VERSION
    assert await _header(db_path) == 0


async def test_sqlite_legacy_header_of_older_version_raises(tmp_path):
    db_path = str(tmp_path / "legacy-old.db")
    await _make_legacy_db(db_path, 1)

    with pytest.raises(SchemaVersionError, match="schema version 1") as excinfo:
        await _ensure_schema(db_path)
    assert (excinfo.value.found, excinfo.value.expected) == (1, SCHEMA_VERSION)


async def test_sqlite_dropping_llmbroker_tables_recovers_from_a_stale_header(tmp_path):
    """The reported bug: a host recovers from a version mismatch by dropping the
    llmbroker_* tables, but cannot drop the file header the mismatch lives in."""
    db_path = str(tmp_path / "dropped.db")
    await _make_legacy_db(db_path, 1)
    async with aiosqlite.connect(db_path) as db:
        names = await (
            await db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name GLOB 'llmbroker_*'",
            )
        ).fetchall()
        for (name,) in names:
            await db.execute(f"DROP TABLE {name}")
        await db.commit()

    await _ensure_schema(db_path)

    assert await _marker(db_path) == SCHEMA_VERSION
    assert await _header(db_path) == 1


async def test_sqlite_never_touches_a_host_owned_header(tmp_path):
    db_path = str(tmp_path / "host-owned.db")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("CREATE TABLE host_migrations (id INTEGER PRIMARY KEY)")
        await db.execute("PRAGMA user_version = 42")
        await db.commit()

    await SqliteDriver(db_path).ensure_schema()

    assert await _marker(db_path) == SCHEMA_VERSION
    assert await _header(db_path) == 42


async def test_sqlite_reopening_a_marker_database_leaves_the_header_alone(tmp_path):
    db_path = str(tmp_path / "host-owned-reopen.db")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA user_version = 42")
        await db.commit()

    await SqliteDriver(db_path).ensure_schema()
    await _ensure_schema(db_path)

    assert await _marker(db_path) == SCHEMA_VERSION
    assert await _header(db_path) == 42


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
        with pytest.raises(SchemaVersionError, match="schema version 1") as excinfo:
            await PostgresDriver(pg_pool).ensure_schema()
        assert (excinfo.value.found, excinfo.value.expected) == (1, SCHEMA_VERSION)
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
        with pytest.raises(SchemaVersionError, match="schema version 1") as excinfo:
            await MongoDriver(mongo_db).ensure_schema()
        assert (excinfo.value.found, excinfo.value.expected) == (1, SCHEMA_VERSION)
    finally:
        # Restore a clean, current-version marker so later tests sharing this
        # session-scoped database don't inherit the deliberately-broken state.
        await mongo_db["llmbroker_schema_version"].replace_one(
            {},
            {"version": SCHEMA_VERSION},
            upsert=True,
        )

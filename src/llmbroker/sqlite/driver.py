"""SQLite ``Driver``: renders DDL and DML from ``backends.spec.TABLES``. Schema
policy — create-if-missing, fail fast on a version-marker mismatch — is in
``rules/backends.md``."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from llmbroker.backends.driver import Key, Row
from llmbroker.backends.spec import SCHEMA_VERSION, TABLES, TableSpec
from llmbroker.exceptions import SchemaVersionError

_SQL_TYPES = {"text": "TEXT", "int": "INTEGER", "real": "REAL", "json": "TEXT", "timestamp": "TEXT"}

_VERSION_TABLE = "llmbroker_schema_version"

_CREATE_VERSION_TABLE = """\
CREATE TABLE IF NOT EXISTS llmbroker_schema_version (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
)\
"""

_UPSERT_VERSION = """\
INSERT INTO llmbroker_schema_version (id, version)
VALUES (1, ?)
ON CONFLICT (id) DO UPDATE SET version = excluded.version\
"""

# Path-keyed, not id(connection)-keyed: many short-lived aiosqlite connections
# are opened per call against the same file, so the cache must survive them.
_schema_ready: dict[str, int] = {}


def _iso(value: datetime) -> str:
    """Timestamps are TEXT and compared lexicographically, so a stored instant must
    always print in the same offset — otherwise ordering and range bounds compare
    printed wall clocks instead of instants."""
    return (value.astimezone(UTC) if value.tzinfo is not None else value).isoformat()


def _encode(value: object, col_type: str) -> object:
    if value is None:
        return None
    if col_type == "json":
        return json.dumps(value)
    if col_type == "timestamp":
        return _iso(value) if isinstance(value, datetime) else value
    return value


def _decode(value: object, col_type: str) -> object:
    if value is None:
        return None
    if col_type == "json":
        return json.loads(value)  # type: ignore[arg-type]
    if col_type == "timestamp":
        return datetime.fromisoformat(value)  # type: ignore[arg-type]
    return value


def _encode_row(row: Row, spec: TableSpec) -> dict[str, object]:
    return {col: _encode(row.get(col), ty) for col, ty in spec.columns.items()}


def _decode_row(values: tuple, spec: TableSpec) -> Row:
    return {col: _decode(v, spec.columns[col]) for col, v in zip(spec.columns, values, strict=True)}


def _create_table_sql(spec: TableSpec) -> str:
    columns = ", ".join(f"{col} {_SQL_TYPES[ty]}" for col, ty in spec.columns.items())
    return f"CREATE TABLE IF NOT EXISTS {spec.name} ({columns})"


async def _has_table(db: aiosqlite.Connection, name: str) -> bool:
    cursor = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    )
    return await cursor.fetchone() is not None


async def _has_store_tables(db: aiosqlite.Connection) -> bool:
    # GLOB, not LIKE: "_" is a single-character wildcard in LIKE.
    cursor = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table'"
        " AND name GLOB 'llmbroker_*' AND name <> ?",
        (_VERSION_TABLE,),
    )
    return await cursor.fetchone() is not None


async def _marker_version(db: aiosqlite.Connection) -> int | None:
    cursor = await db.execute("SELECT version FROM llmbroker_schema_version WHERE id = 1")
    row = await cursor.fetchone()
    return int(row[0]) if row else None


async def _header_version(db: aiosqlite.Connection) -> int:
    cursor = await db.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def _apply_ddl(db: aiosqlite.Connection) -> None:
    marker_table = await _has_table(db, _VERSION_TABLE)
    marker = await _marker_version(db) if marker_table else None
    # The file header carries llmbroker's version only where an older release wrote
    # it; with no llmbroker tables, a non-zero value is likely the application's own.
    legacy = 0
    if not marker_table and await _has_store_tables(db):
        legacy = await _header_version(db)
    current = marker if marker is not None else legacy
    if current not in (0, SCHEMA_VERSION):
        raise SchemaVersionError(
            f"llmbroker schema version {current} found, this release expects"
            f" {SCHEMA_VERSION} — drop the llmbroker_* tables and restart"
            " (export registry/secrets/calls first if you need them)",
            found=current,
            expected=SCHEMA_VERSION,
        )
    await db.execute(_CREATE_VERSION_TABLE)
    for spec in TABLES.values():
        await db.execute(_create_table_sql(spec))
        if spec.key:
            cols = ", ".join(spec.key)
            await db.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {spec.name}_unique ON {spec.name}({cols})",
            )
        for idx_cols in spec.indexes:
            idx_name = f"{spec.name}_idx_{'_'.join(idx_cols)}"
            cols = ", ".join(idx_cols)
            await db.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {spec.name}({cols})")
    if marker is None:
        await db.execute(_UPSERT_VERSION, (SCHEMA_VERSION,))
    if legacy:
        await db.execute("PRAGMA user_version = 0")


class SqliteDriver:
    """One SQLite file (or one process-local ``:memory:`` database) backing
    every ``llmbroker_*`` table."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        # A ":memory:" database lives only as long as one connection, so the
        # file-path pattern would give every call its own empty database.
        self._memory_conn: aiosqlite.Connection | None = None

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[aiosqlite.Connection]:
        if self._db_path == ":memory:":
            if self._memory_conn is None:
                self._memory_conn = await aiosqlite.connect(":memory:")
            yield self._memory_conn
        else:
            async with aiosqlite.connect(self._db_path) as db:
                yield db

    async def ensure_schema(self) -> None:
        """A real file path migrates under its own ``isolation_level=None`` connection
        and a ``BEGIN IMMEDIATE`` transaction, so concurrent OS processes serialize on
        sqlite's file lock instead of racing. ``:memory:`` has no cross-process concern."""
        if self._db_path and self._db_path != ":memory:":
            if _schema_ready.get(self._db_path) == SCHEMA_VERSION:
                return
            async with aiosqlite.connect(self._db_path, isolation_level=None) as db:
                await db.execute("BEGIN IMMEDIATE")
                try:
                    await _apply_ddl(db)
                    await db.execute("COMMIT")
                except BaseException:
                    await db.execute("ROLLBACK")
                    raise
            _schema_ready[self._db_path] = SCHEMA_VERSION
        else:
            async with self._connection() as db:
                await _apply_ddl(db)
                await db.commit()

    async def fetch(self, table: str) -> list[Row]:
        spec = TABLES[table]
        await self.ensure_schema()
        cols = ", ".join(spec.columns)
        order = ", ".join(spec.key) if spec.key else cols
        query = f"SELECT {cols} FROM {spec.name} ORDER BY {order}"  # noqa: S608
        async with self._connection() as db:
            rows = await (await db.execute(query)).fetchall()
        return [_decode_row(r, spec) for r in rows]

    async def get(self, table: str, key: Key) -> Row | None:
        spec = TABLES[table]
        await self.ensure_schema()
        cols = ", ".join(spec.columns)
        where = " AND ".join(f"{k} = ?" for k in spec.key)
        async with self._connection() as db:
            row = await (
                await db.execute(f"SELECT {cols} FROM {spec.name} WHERE {where}", key)  # noqa: S608
            ).fetchone()
        return _decode_row(row, spec) if row is not None else None

    async def upsert(self, table: str, key: Key, row: Row) -> None:  # noqa: ARG002
        """``key`` is part of the Driver contract but unused here: the row already
        carries its own key-column values, which ON CONFLICT matches against."""
        spec = TABLES[table]
        await self.ensure_schema()
        encoded = _encode_row(row, spec)
        cols = list(spec.columns)
        placeholders = ", ".join("?" for _ in cols)
        conflict = ", ".join(spec.key)
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in spec.key)
        # Identifiers below come from the fixed internal TableSpec, not user input.
        query = (
            f"INSERT INTO {spec.name} ({', '.join(cols)}) VALUES ({placeholders})"  # noqa: S608
            f" ON CONFLICT({conflict}) DO UPDATE SET {updates}"
        )
        async with self._connection() as db:
            await db.execute(query, [encoded[c] for c in cols])
            await db.commit()

    async def delete(self, table: str, key: Key) -> bool:
        spec = TABLES[table]
        await self.ensure_schema()
        where = " AND ".join(f"{k} = ?" for k in spec.key)
        async with self._connection() as db:
            cursor = await db.execute(f"DELETE FROM {spec.name} WHERE {where}", key)  # noqa: S608
            await db.commit()
            return cursor.rowcount > 0

    async def append(self, table: str, row: Row) -> None:
        spec = TABLES[table]
        await self.ensure_schema()
        encoded = _encode_row(row, spec)
        cols = list(spec.columns)
        placeholders = ", ".join("?" for _ in cols)
        query = f"INSERT INTO {spec.name} ({', '.join(cols)}) VALUES ({placeholders})"  # noqa: S608
        async with self._connection() as db:
            await db.execute(query, [encoded[c] for c in cols])
            await db.commit()

    async def recent(
        self,
        table: str,
        limit: int,
        match: Row | None = None,
        since: datetime | None = None,
    ) -> list[Row]:
        spec = TABLES[table]
        await self.ensure_schema()
        cols = ", ".join(spec.columns)
        params: list[object] = []
        conditions: list[str] = []
        if match:
            # "=" never matches NULL in SQL; a None filter value means "IS NULL".
            for k, v in match.items():
                if v is None:
                    conditions.append(f"{k} IS NULL")
                else:
                    conditions.append(f"{k} = ?")
                    params.append(v)
        if since is not None:
            conditions.append("called_at >= ?")
            params.append(_iso(since))
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        params.append(limit)
        async with self._connection() as db:
            rows = await (
                await db.execute(
                    f"SELECT {cols} FROM {spec.name}{where} ORDER BY called_at DESC LIMIT ?",  # noqa: S608
                    params,
                )
            ).fetchall()
        return [_decode_row(r, spec) for r in rows]

    async def purge(self, table: str, before: datetime) -> int:
        spec = TABLES[table]
        await self.ensure_schema()
        async with self._connection() as db:
            cursor = await db.execute(
                f"DELETE FROM {spec.name} WHERE called_at < ?",  # noqa: S608
                [_iso(before)],
            )
            await db.commit()
            return cursor.rowcount

    async def aclose(self) -> None:
        if self._memory_conn is not None:
            conn, self._memory_conn = self._memory_conn, None
            await conn.close()

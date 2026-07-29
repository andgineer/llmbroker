"""Postgres ``Driver``: renders DDL and DML from ``backends.spec.TABLES``.

The caller owns the pool (``pool = await asyncpg.create_pool(dsn)``); ``aclose()``
is a no-op. One known installation, upgraded manually — on a fresh database the
schema is created and stamped; on a version-marker mismatch ``ensure_schema``
fails fast instead of attempting an in-place migration.
"""

import asyncio
import json
from datetime import datetime

import asyncpg

from llmbroker.backends.driver import Key, Row
from llmbroker.backends.spec import SCHEMA_VERSION, TABLES, TableSpec
from llmbroker.exceptions import SchemaVersionError

_SQL_TYPES = {
    "text": "TEXT",
    "int": "INTEGER",
    "real": "DOUBLE PRECISION",
    "json": "JSONB",
    "timestamp": "TIMESTAMPTZ",
}

_CREATE_VERSION_TABLE = """\
CREATE TABLE IF NOT EXISTS llmbroker_schema_version (
    id      SMALLINT PRIMARY KEY DEFAULT 1,
    version INTEGER  NOT NULL,
    CHECK (id = 1)
)\
"""

_UPSERT_VERSION = """\
INSERT INTO llmbroker_schema_version (id, version)
VALUES (1, $1)
ON CONFLICT (id) DO UPDATE SET version = EXCLUDED.version\
"""


def _encode(value: object, col_type: str) -> object:
    if value is None:
        return None
    if col_type == "json":
        return json.dumps(value)
    return value


def _decode(value: object, col_type: str) -> object:
    if value is None:
        return None
    if col_type == "json":
        return json.loads(value)  # type: ignore[arg-type]
    return value


def _encode_row(row: Row, spec: TableSpec) -> dict[str, object]:
    return {col: _encode(row.get(col), ty) for col, ty in spec.columns.items()}


def _decode_row(record: asyncpg.Record, spec: TableSpec) -> Row:
    return {col: _decode(record[col], ty) for col, ty in spec.columns.items()}


def _placeholder(index: int, col_type: str) -> str:
    return f"${index}::jsonb" if col_type == "json" else f"${index}"


def _create_table_sql(spec: TableSpec) -> str:
    columns = ", ".join(f"{col} {_SQL_TYPES[ty]}" for col, ty in spec.columns.items())
    return f"CREATE TABLE IF NOT EXISTS {spec.name} ({columns})"


async def _apply_ddl(conn: asyncpg.pool.PoolConnectionProxy) -> None:
    await conn.execute(_CREATE_VERSION_TABLE)
    row = await conn.fetchrow("SELECT version FROM llmbroker_schema_version WHERE id = 1")
    current = int(row["version"]) if row else 0
    if current not in (0, SCHEMA_VERSION):
        raise SchemaVersionError(
            f"llmbroker schema version {current} found, this release expects"
            f" {SCHEMA_VERSION} — drop the llmbroker_* tables and restart"
            " (export registry/secrets/calls first if you need them)",
            found=current,
            expected=SCHEMA_VERSION,
        )
    for spec in TABLES.values():
        await conn.execute(_create_table_sql(spec))
        if spec.key:
            cols = ", ".join(spec.key)
            await conn.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {spec.name}_unique ON {spec.name}({cols})",
            )
        for idx_cols in spec.indexes:
            idx_name = f"{spec.name}_idx_{'_'.join(idx_cols)}"
            cols = ", ".join(idx_cols)
            await conn.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {spec.name}({cols})")
    if current == 0:
        await conn.execute(_UPSERT_VERSION, SCHEMA_VERSION)


class PostgresDriver:
    """One asyncpg pool backing every ``llmbroker_*`` table.

    Pass a pre-built ``pool`` when the caller owns it (``aclose()`` stays a
    no-op, unchanged contract). Pass ``dsn=`` instead to let the driver create
    and own its own pool lazily on first use (needed because pool creation is
    async but this constructor is not) — ``aclose()`` then closes it.
    """

    def __init__(self, pool: asyncpg.Pool | None = None, *, dsn: str | None = None) -> None:
        if pool is None and dsn is None:
            raise ValueError("PostgresDriver requires either pool= or dsn=")
        self._pool = pool
        self._dsn = dsn
        self._owns_pool = pool is None
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()

    async def ensure_schema(self) -> None:
        """A plain per-instance flag: one driver per stack is app-lifetime, so this
        gives one check per process under intended usage. Not ``id(pool)``-keyed —
        after GC an id can be reused by a different pool and falsely skip creation."""
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            if self._pool is None:
                self._pool = await asyncpg.create_pool(self._dsn)
            async with self._pool.acquire() as conn, conn.transaction():
                await _apply_ddl(conn)
            self._schema_ready = True

    async def fetch(self, table: str) -> list[Row]:
        spec = TABLES[table]
        await self.ensure_schema()
        cols = ", ".join(spec.columns)
        order = ", ".join(spec.key) if spec.key else cols
        async with self._pool.acquire() as conn:
            records = await conn.fetch(f"SELECT {cols} FROM {spec.name} ORDER BY {order}")  # noqa: S608
        return [_decode_row(r, spec) for r in records]

    async def get(self, table: str, key: Key) -> Row | None:
        spec = TABLES[table]
        await self.ensure_schema()
        cols = ", ".join(spec.columns)
        where = " AND ".join(f"{k} = ${i}" for i, k in enumerate(spec.key, start=1))
        async with self._pool.acquire() as conn:
            record = await conn.fetchrow(f"SELECT {cols} FROM {spec.name} WHERE {where}", *key)  # noqa: S608
        return _decode_row(record, spec) if record is not None else None

    async def upsert(self, table: str, key: Key, row: Row) -> None:  # noqa: ARG002
        """``key`` is part of the Driver contract but unused here: the row already
        carries its own key-column values, which ON CONFLICT matches against."""
        spec = TABLES[table]
        await self.ensure_schema()
        encoded = _encode_row(row, spec)
        cols = list(spec.columns)
        placeholders = ", ".join(_placeholder(i, spec.columns[c]) for i, c in enumerate(cols, 1))
        conflict = ", ".join(spec.key)
        updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in spec.key)
        # Identifiers below come from the fixed internal TableSpec, not user input.
        query = (
            f"INSERT INTO {spec.name} ({', '.join(cols)}) VALUES ({placeholders})"  # noqa: S608
            f" ON CONFLICT ({conflict}) DO UPDATE SET {updates}"
        )
        async with self._pool.acquire() as conn:
            await conn.execute(query, *(encoded[c] for c in cols))

    async def delete(self, table: str, key: Key) -> bool:
        spec = TABLES[table]
        await self.ensure_schema()
        where = " AND ".join(f"{k} = ${i}" for i, k in enumerate(spec.key, start=1))
        async with self._pool.acquire() as conn:
            result = await conn.execute(f"DELETE FROM {spec.name} WHERE {where}", *key)  # noqa: S608
        return result.split()[-1] != "0"

    async def append(self, table: str, row: Row) -> None:
        spec = TABLES[table]
        await self.ensure_schema()
        encoded = _encode_row(row, spec)
        cols = list(spec.columns)
        placeholders = ", ".join(_placeholder(i, spec.columns[c]) for i, c in enumerate(cols, 1))
        query = f"INSERT INTO {spec.name} ({', '.join(cols)}) VALUES ({placeholders})"  # noqa: S608
        async with self._pool.acquire() as conn:
            await conn.execute(query, *(encoded[c] for c in cols))

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
            for k, v in match.items():
                if v is None:
                    conditions.append(f"{k} IS NULL")
                else:
                    params.append(v)
                    conditions.append(f"{k} = ${len(params)}")
        if since is not None:
            params.append(since)
            conditions.append(f"called_at >= ${len(params)}")
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        params.append(limit)
        async with self._pool.acquire() as conn:
            records = await conn.fetch(
                f"SELECT {cols} FROM {spec.name}{where}"  # noqa: S608
                f" ORDER BY called_at DESC LIMIT ${len(params)}",
                *params,
            )
        return [_decode_row(r, spec) for r in records]

    async def purge(self, table: str, before: datetime) -> int:
        spec = TABLES[table]
        await self.ensure_schema()
        async with self._pool.acquire() as conn:
            result = await conn.execute(f"DELETE FROM {spec.name} WHERE called_at < $1", before)  # noqa: S608
        return int(result.split()[-1])

    async def aclose(self) -> None:
        if self._owns_pool and self._pool is not None:
            pool, self._pool = self._pool, None
            await pool.close()

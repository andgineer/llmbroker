"""MongoDB ``Driver``: renders indexes from ``backends.spec.TABLES``.

One known installation, upgraded manually — on a version-marker mismatch
``ensure_schema`` fails fast instead of attempting an in-place migration.
"""

import asyncio
from datetime import UTC, datetime

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from llmbroker.backends.driver import Key, Row
from llmbroker.backends.spec import SCHEMA_VERSION, TABLES, TableSpec
from llmbroker.exceptions import SchemaVersionError


def _ensure_utc(dt: datetime | None) -> datetime | None:
    """The client is never opened with ``tz_aware=True``, so pymongo returns naive
    datetimes for BSON dates — reattach UTC rather than misreading them as local."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _decode(value: object, col_type: str) -> object:
    if col_type == "timestamp" and isinstance(value, datetime):
        return _ensure_utc(value)
    return value


def _decode_doc(doc: dict, spec: TableSpec) -> Row:
    return {col: _decode(doc.get(col), ty) for col, ty in spec.columns.items()}


def _key_filter(spec: TableSpec, key: Key) -> dict[str, object]:
    return dict(zip(spec.key, key, strict=True))


class MongoDriver:
    """One Mongo database backing every ``llmbroker_*`` collection."""

    def __init__(
        self,
        db: AsyncIOMotorDatabase,
        *,
        client: AsyncIOMotorClient | None = None,
    ) -> None:
        """Pass ``client=`` only when the driver should own (and close) it — the
        explicit-``db`` facade use case does not own the caller's client."""
        self._db = db
        self._client = client
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()

    async def ensure_schema(self) -> None:
        """A plain per-instance flag: one driver per stack is app-lifetime, so this
        gives one check per process under intended usage. Not ``id(db)``-keyed —
        after GC an id can be reused by a different database and falsely skip creation."""
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            version_doc = await self._db["llmbroker_schema_version"].find_one({})
            current = int(version_doc["version"]) if version_doc else 0
            if current not in (0, SCHEMA_VERSION):
                raise SchemaVersionError(
                    f"llmbroker schema version {current} found, this release expects"
                    f" {SCHEMA_VERSION} — drop the llmbroker_* collections and restart"
                    " (export registry/secrets/calls first if you need them)",
                    found=current,
                    expected=SCHEMA_VERSION,
                )
            for spec in TABLES.values():
                if spec.key:
                    await self._db[spec.name].create_index(
                        [(k, 1) for k in spec.key],
                        unique=True,
                        name=f"{spec.name}_unique",
                    )
                for idx_cols in spec.indexes:
                    await self._db[spec.name].create_index(
                        [(k, 1) for k in idx_cols],
                        name=f"{spec.name}_idx_{'_'.join(idx_cols)}",
                    )
            if current == 0:
                await self._db["llmbroker_schema_version"].replace_one(
                    {},
                    {"version": SCHEMA_VERSION},
                    upsert=True,
                )
            self._schema_ready = True

    async def fetch(self, table: str) -> list[Row]:
        spec = TABLES[table]
        await self.ensure_schema()
        sort = [(k, 1) for k in spec.key] if spec.key else None
        cursor = self._db[spec.name].find({})
        if sort:
            cursor = cursor.sort(sort)
        docs = await cursor.to_list(length=None)
        return [_decode_doc(d, spec) for d in docs]

    async def get(self, table: str, key: Key) -> Row | None:
        spec = TABLES[table]
        await self.ensure_schema()
        doc = await self._db[spec.name].find_one(_key_filter(spec, key))
        return _decode_doc(doc, spec) if doc is not None else None

    async def upsert(self, table: str, key: Key, row: Row) -> None:
        spec = TABLES[table]
        await self.ensure_schema()
        await self._db[spec.name].update_one(
            _key_filter(spec, key),
            {"$set": row},
            upsert=True,
        )

    async def delete(self, table: str, key: Key) -> bool:
        spec = TABLES[table]
        await self.ensure_schema()
        result = await self._db[spec.name].delete_one(_key_filter(spec, key))
        return result.deleted_count > 0

    async def append(self, table: str, row: Row) -> None:
        spec = TABLES[table]
        await self.ensure_schema()
        await self._db[spec.name].insert_one(dict(row))

    async def recent(
        self,
        table: str,
        limit: int,
        match: Row | None = None,
        since: datetime | None = None,
    ) -> list[Row]:
        spec = TABLES[table]
        await self.ensure_schema()
        query: dict[str, object] = dict(match) if match else {}
        if since is not None:
            query["called_at"] = {"$gte": since}
        cursor = self._db[spec.name].find(query).sort("called_at", -1).limit(limit)
        docs = await cursor.to_list(length=None)
        return [_decode_doc(d, spec) for d in docs]

    async def purge(self, table: str, before: datetime) -> int:
        spec = TABLES[table]
        await self.ensure_schema()
        result = await self._db[spec.name].delete_many({"called_at": {"$lt": before}})
        return int(result.deleted_count)

    async def aclose(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            client.close()

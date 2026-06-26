# Plan: Postgres and MongoDB backends

Source of truth: https://github.com/andgineer/llmbroker/issues/2

## Scope

Add `llmbroker.postgres` and `llmbroker.mongodb` subpackages, each providing the
same four batteries as `llmbroker.sqlite`:

| Class | Protocol |
|---|---|
| `Registry` | `MutableRegistryProtocol` |
| `Secrets` | `MutableSecretsProtocol` |
| `StateStore` | `StateStoreProtocol` |
| `Telemetry` | `QueryableTelemetryProtocol` |

---

## Step 1 — `pyproject.toml`: add optional extras

Add an `[project.optional-dependencies]` section (none exists today):

```toml
[project.optional-dependencies]
postgres = ["asyncpg>=0.30.0"]
mongodb  = ["motor>=3.6.0"]
```

`aiosqlite` stays in `[project.dependencies]` (already there; this issue does not
refactor SQLite into an extra).

---

## Step 2 — `llmbroker.postgres` subpackage

### File layout

```
src/llmbroker/postgres/
    __init__.py       # exports Registry, Secrets, StateStore, Telemetry
    schema.py         # ensure_schema — sole DDL owner
    registry.py
    secrets.py
    state_store.py
    telemetry.py
```

### Connection contract

Each class accepts an `asyncpg.Pool` in its constructor. The caller owns pool
lifecycle. `aclose()` on the class is a no-op (pool is caller-owned).

```python
class StateStore:
    def __init__(self, pool: asyncpg.Pool) -> None: ...
    async def aclose(self) -> None: ...  # no-op; pool is caller-owned
```

### `schema.py`

Schema version tracked via a `llmbroker_schema_version` table (no `PRAGMA` in
Postgres). `ensure_schema` is idempotent — keyed by `id(pool)`.

```sql
CREATE TABLE IF NOT EXISTS llmbroker_schema_version (
    id      SMALLINT PRIMARY KEY DEFAULT 1,
    version INTEGER  NOT NULL,
    CHECK (id = 1)       -- enforces single-row
);

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
    id             BIGSERIAL PRIMARY KEY,
    llm_name       TEXT NOT NULL,
    phase          TEXT NOT NULL,
    cooldown_until TIMESTAMPTZ,
    fail_count     INTEGER NOT NULL DEFAULT 0,
    user_id        TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS llmbroker_state_unique
    ON llmbroker_state(llm_name, COALESCE(user_id, ''));
```

Version guard pattern:

```python
_SCHEMA_VERSION = 1
_schema_ready: set[int] = set()  # id(pool) values already initialised
_schema_lock = asyncio.Lock()    # prevents concurrent initialisations on the same pool

async def ensure_schema(pool: asyncpg.Pool) -> None:
    if id(pool) in _schema_ready:
        return
    async with _schema_lock:
        if id(pool) in _schema_ready:   # double-check after acquiring lock
            return
        async with pool.acquire() as conn:
            async with conn.transaction():
                # execute all CREATE TABLE / INDEX statements above
                # INSERT INTO llmbroker_schema_version(id, version)
                # VALUES (1, _SCHEMA_VERSION)
                # ON CONFLICT (id) DO UPDATE SET version = EXCLUDED.version
        _schema_ready.add(id(pool))
# Known limitation: id() may be reused if a pool is GC'd and a new one is
# created. Pools are long-lived in practice so the risk is negligible.
```

### Parameter style

asyncpg uses positional `$1 $2 …` placeholders, not `?`.

### Upsert / conflict patterns

- **state write**: `INSERT … ON CONFLICT (llm_name, COALESCE(user_id, '')) DO UPDATE SET …`
- **registry add**: plain `INSERT`; catch `asyncpg.UniqueViolationError` → raise `ValueError`
- **registry update / remove**: check `rowcount`; `0` → raise `KeyError`
- **secrets set**: `INSERT … ON CONFLICT (ref, COALESCE(user_id, '')) DO UPDATE SET value = EXCLUDED.value`

### Alembic coexistence

`llmbroker.integrations.alembic` is a pre-existing module (not created by this
issue). Its `include_object` function already filters every `llmbroker_*`
object — no new hook needed. Document in `__init__.py` docstring that users
wire the existing hook into `alembic/env.py`.

---

## Step 3 — `llmbroker.mongodb` subpackage

### File layout

```
src/llmbroker/mongodb/
    __init__.py       # exports Registry, Secrets, StateStore, Telemetry
    schema.py         # ensure_schema — index owner
    registry.py
    secrets.py
    state_store.py
    telemetry.py
```

### Connection contract

Each class accepts a `motor.motor_asyncio.AsyncIOMotorDatabase`.

```python
class StateStore:
    def __init__(self, db: AsyncIOMotorDatabase) -> None: ...
    async def aclose(self) -> None: ...  # no-op
```

### Collection names

| Purpose | Collection |
|---|---|
| Registry | `llmbroker_registry` |
| Calls | `llmbroker_calls` |
| Secrets | `llmbroker_secrets` |
| State | `llmbroker_state` |

### `schema.py`

`ensure_schema` creates indexes (idempotent by default in MongoDB) and writes a
version document to `llmbroker_schema_version`. Keyed by `id(db)`.

```python
_SCHEMA_VERSION = 1
_schema_ready: set[int] = set()

async def ensure_schema(db: AsyncIOMotorDatabase) -> None:
    if id(db) in _schema_ready:
        return
    await db["llmbroker_registry"].create_index(
        [("name", 1), ("user_id", 1)], unique=True, name="llmbroker_registry_unique"
    )
    await db["llmbroker_calls"].create_index([("llm_name", 1)], name="llmbroker_idx_calls_llm_name")
    await db["llmbroker_calls"].create_index([("called_at", -1)], name="llmbroker_idx_calls_called_at")
    await db["llmbroker_calls"].create_index([("user_id", 1)], name="llmbroker_idx_calls_user_id")
    await db["llmbroker_secrets"].create_index(
        [("ref", 1), ("user_id", 1)], unique=True, name="llmbroker_secrets_unique"
    )
    await db["llmbroker_state"].create_index(
        [("llm_name", 1), ("user_id", 1)], unique=True, name="llmbroker_state_unique"
    )
    await db["llmbroker_schema_version"].replace_one(
        {}, {"version": _SCHEMA_VERSION}, upsert=True
    )
    _schema_ready.add(id(db))
```

### Document shapes

**llmbroker_state**: `{llm_name, phase, cooldown_until (datetime|null), fail_count, user_id}`

**llmbroker_registry**: `{name, base_url, model, api_key_ref, user_id}`

**llmbroker_calls**: all `Call` fields; `called_at` stored as native `datetime`
(motor serialises to BSON Date); `usage_extra` stored as a BSON sub-document
(native `dict`) — unlike Postgres which stores it as a JSON string and must
deserialise on read. Both backends must return `usage_extra` as `dict | None`
to the caller; serialisation/deserialisation is internal to each backend.

**llmbroker_secrets**: `{ref, value, user_id}`

### Upsert / conflict patterns

- **state write**: `replace_one({llm_name, user_id}, doc, upsert=True)`
- **registry add**: `insert_one`; catch `DuplicateKeyError` → raise `ValueError`
- **registry update**: `replace_one({name, user_id})`; `matched_count == 0` → `KeyError`
- **registry remove**: `delete_one({name, user_id})`; `deleted_count == 0` → `KeyError`
- **secrets set**: `replace_one({ref, user_id}, doc, upsert=True)`

### `user_id` scoping

MongoDB has no `IS NULL`. Store `user_id: None` explicitly on insert so the
unique index covers both scoped and unscoped rows uniformly. All query filters
use `{"user_id": None}` for unscoped; Python `None` maps directly to BSON null.

---

## Step 4 — Tests

### Location

```
tests/conftest.py               # pg_pool and mongo_db session fixtures
tests/test_postgres_registry.py
tests/test_postgres_secrets.py
tests/test_postgres_state_store.py
tests/test_postgres_telemetry.py

tests/test_mongodb_registry.py
tests/test_mongodb_secrets.py
tests/test_mongodb_state_store.py
tests/test_mongodb_telemetry.py
```

### asyncio mode

Add to `pytest.ini`:
```ini
asyncio_mode = auto
```
This enables async fixtures and test functions without per-test `@pytest.mark.asyncio`.

### Skip strategy

Use `pytest.importorskip` at module level in each test file to skip if the
driver is absent. Shared session-scoped async fixtures live in `conftest.py`
and call `pytest.skip` if the server is unreachable:

```python
# conftest.py
import pytest

@pytest.fixture(scope="session")
async def pg_pool():
    asyncpg = pytest.importorskip("asyncpg")  # inside fixture — does not skip conftest
    try:
        pool = await asyncpg.create_pool("postgresql://localhost/llmbroker_test")
    except Exception:
        pytest.skip("postgres not available")
    yield pool
    await pool.close()


@pytest.fixture(scope="session")
async def mongo_db():
    motor_asyncio = pytest.importorskip("motor.motor_asyncio")
    try:
        client = motor_asyncio.AsyncIOMotorClient(
            "mongodb://localhost:27017", serverSelectionTimeoutMS=2000
        )
        await client.server_info()
    except Exception:
        pytest.skip("mongodb not available")
    yield client["llmbroker_test"]
    client.close()
```

Each test file still calls `pytest.importorskip` at module level (before any test
function) to skip collection of that file when the driver is absent, avoiding
fixture-resolution errors.

### Coverage mirrors SQLite

- CRUD round-trips: `add` / `get` / `update` / `remove` / `load`
- Duplicate `add` → `ValueError`; missing `update` / `remove` → `KeyError`
- `user_id` scoping: rows for user A not visible to user B; `None` scope isolated
- `StateStore`: write then read back; expired `cooldown_until` becomes AVAILABLE
- `Telemetry`: `record`, `record_quality`, `calls(limit=)`, `metrics(since=)`, `purge_calls`
- `Secrets`: `resolve` / `set` / `require_user_id` guard

---

## Step 5 — `__init__.py` docstrings

Each subpackage `__init__.py` follows the SQLite template:

**postgres:**
```
Postgres backend: registry, telemetry, state-store, and secrets.

Needs the ``asyncpg`` driver (``llmbroker[postgres]``); importing this package
is how a host declares that dependency — bare ``import llmbroker`` stays
driver-free. All tables are ``llmbroker_``-prefixed and owned by ``ensure_schema``.

Alembic coexistence: wire ``llmbroker.integrations.alembic.include_object``
into ``alembic/env.py`` as ``context.configure(include_object=...)`` so
autogenerate skips every ``llmbroker_*`` object.
```

**mongodb:**
```
MongoDB backend: registry, telemetry, state-store, and secrets.

Needs the ``motor`` driver (``llmbroker[mongodb]``); importing this package
is how a host declares that dependency — bare ``import llmbroker`` stays
driver-free. All collections are ``llmbroker_``-prefixed and owned by
``ensure_schema``.
```

---

## Invariants carried from SQLite

- Every class implements `aclose() -> None` (satisfies `AsyncResourceProtocol`)
- `check_user_id(user_id)` called at the top of every method that accepts `user_id`
- `user_id=""` always raises `ValueError` (enforced by `check_user_id`)
- `purge_calls` is cross-tenant — no `user_id` filter
- `record_quality` raises `KeyError` for an unknown `call_id`
- Duplicate `registry.add` raises `ValueError`; missing `update`/`remove` raises `KeyError`

---

## Out of scope

- Moving `aiosqlite` from core deps to a `sqlite` extra
- Redis backend (issue #1)
- Optimizer warm-start wiring into `Telemetry.metrics` (P4)

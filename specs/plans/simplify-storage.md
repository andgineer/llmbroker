# Simplify the storage layer: one driver per DB, ports written once (Plan 3 of 3)

Prerequisites: `specs/plans/simplify-core.md` (Plan 1) and
`specs/plans/simplify-learning.md` (Plan 2) are fully landed and green. Plan 2
already removed the summaries/fold machinery, so the state store here is
**cooldowns only** — plain keyed records, no atomic arithmetic anywhere.

Zero functional change. Public API stays intact: `AsyncBroker`/`Broker` surface,
subpackage class names (`llmbroker.sqlite.Registry`, …), every port protocol.

Expected outcome: backend code shrinks to roughly one driver file (~150–200
lines) per DB plus one shared ports module; adding a new DB backend = one driver
file. Per-call DB cost: one telemetry insert, one journal-rebuild read per 60s
activity window (Plan 2), state reads/writes only around failures (cached 2s);
profile writes only on `disable_llm`/`enable_llm`. Idle: 0.

## Rules for the implementer (read first)

- Lint/format/type-check only via `invoke pre` (never call ruff directly). Tests via
  `python -m pytest`. Both must be green after **every numbered step**.
- `pytest.ini` runs `--doctest-modules`: doctests in `src/` execute as tests.
- No in-function imports; no `from __future__ import annotations`; Python 3.11+.
- Never edit `src/llmbroker/__about__.py`; never bump the version.
- Never use `pytest.skip`/`importorskip`/`skipIf`. Postgres/MongoDB testcontainers
  run locally on macOS — tests must run, not skip.
- Never delete a test that reproduces a confirmed bug; port it to the new surface.
- Storage backend packages are optional extras in `pyproject.toml` — never add them
  to the dev group; a bare `import llmbroker` must never pull in a driver package.
- **Never duplicate tests per backend**: behavior is tested once; backends enter
  via parametrized fixtures (`tests/conftest.py`) and the driver conformance suite.
- Comments: 1–2 lines, non-obvious WHY only.

## Problem

Four domain ports × four DBs. Every backend method repeats the same ritual —
`check_user_id` → uid normalization → `ensure_schema` → acquire connection → one
statement → JSON/error translation (see e.g. `postgres/registry.py:40-53`) — and
only the statement is DB-specific. Schema DDL and the user-scoping idiom are
re-written per DB.

---

## 3.1 The `Driver` protocol and declarative schema

New subpackage `src/llmbroker/backends/` (zero external dependencies — importable
by a bare `import llmbroker`).

**`backends/spec.py`** — one declarative description of the stores, consumed by
every driver's `ensure_schema`:

```python
@dataclass(frozen=True)
class TableSpec:
    name: str                      # "llmbroker_registry", ...
    key: tuple[str, ...]           # identity columns, e.g. ("name",); user scope implicit
    columns: dict[str, str]        # portable types: "text" | "int" | "real" | "json" | "timestamp"
    indexes: tuple[tuple[str, ...], ...] = ()

TABLES: dict[str, TableSpec] = {...}   # registry, calls, secrets, state
SCHEMA_VERSION = 5                     # bump: llmbroker_summaries is gone (Plan 2)
```

Column sets mirror the current DDL (`sqlite/schema.py:14-107`) minus the
`llmbroker_summaries` table. Registry keeps `metadata` and `profile` as **two
separate** json columns — after Plan 2 `profile` holds only the manual bench
latch (+ `quality_reset_at`), and the seed path must still never be able to
touch it.

**Schema policy (single known installation).** `ensure_schema` creates missing
tables/indexes and stamps `SCHEMA_VERSION` on a fresh database. On a
version-marker mismatch it raises a clear, actionable error, e.g.
`"llmbroker schema version 4 found, this release expects 5 — drop the llmbroker_*
tables and restart (llmbroker_state is a disposable cache; export
registry/secrets/calls first if you need them)"`. **No upgrade machinery**:
delete the additive `ALTER TABLE` path, the `PRAGMA table_info` column sniffing
(`sqlite/schema.py:133-140`), and the drop-before-create ordering — there is
exactly one known installation, upgraded manually by its operator.

**`backends/driver.py`** — the per-DB contract. Record-shaped, not domain-shaped.
`Row = dict[str, object]`, `Key = tuple[object, ...]`, `Scope = int | str | None`:

```python
class Driver(Protocol):
    async def ensure_schema(self) -> None: ...
    # keyed records (registry, secrets, state)
    async def fetch(self, table: str, scope: Scope) -> list[Row]: ...
        # ordered by key columns — registry load order feeds selection priority,
        # matching today's ORDER BY name (postgres/registry.py:35)
    async def get(self, table: str, key: Key, scope: Scope) -> Row | None: ...
    async def insert(self, table: str, key: Key, row: Row, scope: Scope) -> None: ...  # DuplicateKeyError
    async def upsert(self, table: str, key: Key, row: Row, scope: Scope) -> None: ...
    async def update(self, table: str, key: Key, fields: Row, scope: Scope) -> bool: ...  # False if absent
    async def delete(self, table: str, key: Key, scope: Scope) -> bool: ...
    # journal ops (llmbroker_calls)
    async def append(self, table: str, row: Row) -> None: ...
    async def recent(self, table: str, scope: Scope, limit: int) -> list[Row]: ...
    async def set_field(self, table: str, key: Key, field: str, value: object) -> bool: ...
    async def metrics_rows(self, table: str, scope: Scope, since: datetime | None) -> list[Row]: ...
        # one row per llm_name: {"llm_name", "call_count", "last_status", "last_at"};
        # any correct query shape is fine — carrying over the current
        # per-name last-status lookup (sqlite/telemetry.py:130-143) is acceptable
    async def purge(self, table: str, before: datetime) -> int: ...   # cross-tenant by design
    async def aclose(self) -> None: ...
```

Note there are **no state-specific driver ops**: after Plan 2 the state store is
plain keyed records (`fetch`/`upsert` on `llmbroker_state`), so the generic ops
cover it.

`DuplicateKeyError` lives in `llmbroker/exceptions.py`. The generic layer (not the
drivers) owns: `check_user_id`, lazy one-time `ensure_schema` gating,
JSON⇄dataclass translation, `KeyError`/`ValueError` semantics, `reconcile()` on
state reads. A driver method body is the one statement that is genuinely
DB-specific.

**`ensure_schema` gating**: a plain boolean on the driver instance — checked
before every operation, flipped after the first successful `ensure_schema`. Do
**not** copy the current `id(pool)`-keyed process caches
(`postgres/schema.py:13`, `mongodb/schema.py:14`): after GC an id can be reused
by a different pool and falsely skip creation. One driver per stack is
app-lifetime, so the per-instance flag gives one check per process under intended
usage; sqlite additionally keeps its path-keyed process cache (many short-lived
`Registry("x.db")` objects per process still check once).

Also add `backends/inmemory.py` — a trivial dict-based `Driver` (exported; useful
to users as a test double and to our port unit tests).

## 3.2 Generic ports (written once)

**`backends/ports.py`** — implement the existing port protocols over any `Driver`:

| Class | Implements | Driver ops used |
|---|---|---|
| `StoreRegistry(driver)` | `MutableRegistryProtocol` | fetch/get/insert/update/delete; `update` on the `profile` column only for `read_profiles`/`write_profile` |
| `StoreSecrets(driver, require_user_id=False)` | `MutableSecretsProtocol` | get/upsert (+ the `UserScopeError` guard, written once) |
| `StoreTelemetry(driver)` | `QueryableTelemetryProtocol` | append/recent/set_field/metrics_rows/purge |
| `StoreStateStore(driver)` | `StateStoreProtocol` | fetch/upsert on `llmbroker_state` |

Semantics carried over exactly: `add` create-only → `ValueError` on
`DuplicateKeyError`; `update`/`remove`/`write_profile` → `KeyError` when the driver
returns False; `record_quality` on an unknown call id → `KeyError`
(`sqlite/telemetry.py:104-105`); `purge_calls` ignores scope (admin op); registry
`update` never writes `profile`, `write_profile` never writes `metadata`; state
reads apply `reconcile()` and tz-awareness checks
(`postgres/state_store.py:53-63`).

**Port protocols do not change.** `broker/` and `catalog` are untouched by this
plan.

## 3.3 Concrete drivers and facades

- `sqlite/driver.py` — aiosqlite; renders DDL from `spec.TABLES`; keeps `PRAGMA
  user_version` as the marker, the path-keyed `_schema_ready` process cache, and
  the `BEGIN IMMEDIATE` dedicated-connection pattern (`sqlite/schema.py:143-168`)
  — the latter now only serializes concurrent first-run creates across OS
  processes.
- `postgres/driver.py` — asyncpg; the caller owns the pool, `aclose()` is a no-op
  (unchanged contract); version via the `llmbroker_schema_version` row; the
  `user_id IS NOT DISTINCT FROM $n` scoping idiom written once.
- `mongodb/driver.py` — motor; `user_id: None` stored explicitly in every document
  (unique-index correctness — unchanged); version via the
  `llmbroker_schema_version` document.
- **Redis stays a direct `StateStoreProtocol` implementation** (one port; the
  generic layer buys it nothing). After Plan 2 it shrinks to the cooldown hash
  only: `read` + `write` over `llmbroker_state:{scope}` — delete the summaries
  hash, the CAS fold loop, and the field-separator encoding
  (`redis/state_store.py:57-197`). ~50 lines.
- `aws/secrets.py`, `vault/secrets.py` — unchanged (single-port SDK glue, already
  minimal).

Facades preserve every public name and constructor signature — e.g.
`src/llmbroker/sqlite/__init__.py`:

```python
class Registry(StoreRegistry):
    def __init__(self, db_path: str | Path) -> None:
        super().__init__(SqliteDriver(db_path))
# likewise Secrets (require_user_id passthrough), Telemetry, StateStore
```

Same pattern for `postgres` (wrapping `asyncpg.Pool`) and `mongodb` (wrapping a
Motor database). `BackendStack` builds **one driver** per stack and shares it
across the four ports — simplify its wiring accordingly.

Migrate one DB at a time — sqlite → postgres → mongodb. For each: add the driver,
point the facade at the `Store*` classes, run the full suite, then delete that DB's
superseded `{registry,secrets,telemetry,state_store,schema}.py` modules. No
re-export shims — importers already use the facade names, which survive.

## 3.4 Tests

The suite is already contract-style (fixtures in `tests/conftest.py:118-252`
parametrize each port over its backends) — keep it that way; never a per-backend
copy of a behavior test. Disposition:

| File | Action |
|---|---|
| `tests/test_registry.py`, `test_secrets.py`, `test_telemetry_backends.py`, `test_telemetry.py` | keep as-is — port behavior through the facades, which don't change |
| `tests/test_state_store.py` | shrink to the cooldown-only contract (same parametrized fixture; keep the mongo legacy-datetime and redis repro tests that still apply) |
| `tests/test_schema_migration.py` | shrink: per driver, (a) fresh-DB create is idempotent and stamps the marker, (b) a wrong marker makes `ensure_schema` raise the actionable error |
| new `tests/test_driver_conformance.py` | one suite parametrized over sqlite(file) / sqlite(:memory:) / postgres / mongodb / inmemory drivers: CRUD + `DuplicateKeyError`; scoping exactness (scoped vs unscoped never mix); fetch ordering; journal ops |
| `tests/test_stack.py` | update only if wiring assertions touch internals |
| everything else | unchanged by this plan |

## 3.5 Spec touch-up

`specs/reference/architecture.md`: the four-backend table and battery matrix stay.
In "Where each kind lives", state that dependency-carrying backends are one
storage driver per DB behind shared port logic, and that a custom backend is
either one driver or one full port. State store description: cooldown records
only. Rewrite the "DB schema" section: schema created on first use; version
mismatch fails fast with an actionable error; no in-place upgrades. Remove the
summaries table from the schema list. Specs state the current rule only — no
"previously X, now Y" history.

---

## Step order

1. **3.1–3.2** spec.py + driver protocol + generic ports + in-memory driver (new
   code only — nothing deleted, suite stays green)
2. **3.3** sqlite driver+facade → delete old sqlite modules → postgres → mongodb
   (one DB per step, full suite between); redis shrink
3. **3.4** conformance suite + test consolidation
4. **3.5** spec touch-up

**Final gate:** `invoke pre` + full `python -m pytest` green, zero skips.

# Simplify the storage layer: one driver per DB, ports written once (Plan 3 of 3)

Prerequisites: `specs/plans/simplify-core.md` (Plan 1) and
`specs/plans/simplify-learning.md` (Plan 2) are fully landed and green. Plan 2
removed the summaries/fold machinery **and the state store concept** (shared
cooldowns derive from the journal) — this plan deletes the leftover state-store
modules and the Redis backend, and replaces stacks with the single source
parameter.

Zero functional change beyond the agreed API cuts: `stack=` is replaced by the
source parameter (see 3.3); the Redis extra disappears; the registry protocol
drops its `user_id` parameters (the registry is global — Plan 2 already passes
`None` everywhere, this plan deletes the parameter and the table column); the
secrets protocols drop `user_id` too (Plan 2 moved the scope into the ref
string as a prefix and passes `None` — this plan deletes the parameter, the
column, and the `check_user_id`/`require_user_id`/`UserIdRequired` trio).
Subpackage class names (`llmbroker.sqlite.Registry`, …) and the other port
protocols stay, modulo Plan 2's telemetry→knowledge rename.

Expected outcome: backend code shrinks to roughly one driver file (~150–200
lines) per DB plus one shared ports module; adding a new DB backend = one driver
file. Per-call DB cost: one journal insert, one journal-rebuild read per 60s
activity window (Plan 2 — it also carries shared cooldowns);
registry writes only on `sync`; disabled-map writes only on `set_disabled`
(plus the name-seeding at `sync`);
retention purge at most once per hour of activity (Plan 2 step 2.7). Idle: 0.

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
    key: tuple[str, ...]           # identity columns; every key is a single non-null
                                   # text column: registry ("name",), disabled ("name",),
                                   # secrets ("ref",) — the user scope rides inside the
                                   # ref string (Plan 2), no scope columns exist
    columns: dict[str, str]        # portable types: "text" | "int" | "real" | "json" | "timestamp"
    indexes: tuple[tuple[str, ...], ...] = ()

TABLES: dict[str, TableSpec] = {...}   # registry, calls, disabled, secrets
SCHEMA_VERSION = 5                     # one shared constant replaces the per-DB markers
                                       # (today: sqlite/postgres 4, mongodb 2 — mongodb jumps
                                       # straight to 5). Bump rationale: summaries and state
                                       # tables gone; registry becomes a pure preset mirror
                                       # (user_id, origin, profile columns gone — Plan 2);
                                       # calls gains kind, cooldown_until + key_hash and its
                                       # attribution column is scope (text); secrets keyed by
                                       # ref alone (scope is a ref prefix, Plan 2); new tiny
                                       # llmbroker_disabled (admin verdicts)
```

Column sets mirror the current DDL (`sqlite/schema.py:14-107`) minus the
`llmbroker_summaries` table. The registry table keeps `metadata` as its single
json column and has **no `user_id`, `origin`, `profile`, or `disabled`
columns** — it is a pure mirror of the preset, written only by `sync`
(Plan 2). Admin verdicts live in the new tiny `llmbroker_disabled` table (key
`name`, column `disabled`), written by `set_disabled` and seeded
with model names at `sync` (Plan 2).
The calls table carries `scope` as a plain text attribution column
and gains `kind`, `cooldown_until`, and `key_hash` (Plan 2); quality rows are
self-contained (Plan 2, `kind = "quality"`) — nothing references call rows. The secrets table is
a flat `ref → value` store: no `user_id` column exists anywhere — the scope
is a ref-string prefix built by the broker (Plan 2).

**Schema policy (single known installation).** `ensure_schema` creates missing
tables/indexes and stamps `SCHEMA_VERSION` on a fresh database. On a
version-marker mismatch it raises a clear, actionable error, e.g.
`"llmbroker schema version 4 found, this release expects 5 — drop the llmbroker_*
tables and restart (export registry/secrets/calls first if you need them)"`. **No upgrade machinery**:
delete the additive `ALTER TABLE` path, the `PRAGMA table_info` column sniffing
(`sqlite/schema.py:133-140`), and the drop-before-create ordering — there is
exactly one known installation, upgraded manually by its operator.

**`backends/driver.py`** — the per-DB contract. Record-shaped, not domain-shaped.
`Row = dict[str, object]`, `Key = tuple[object, ...]`. Every key column is
non-null text (the nullable `user_id` secrets key is gone — the scope rides
inside the ref string), so key matching is plain equality — no
`IS NOT DISTINCT FROM` machinery:

```python
class Driver(Protocol):
    async def ensure_schema(self) -> None: ...
    # keyed records (registry, disabled, secrets)
    async def fetch(self, table: str) -> list[Row]: ...
        # ordered by key columns — registry load order feeds selection priority,
        # matching today's ORDER BY name (postgres/registry.py:35)
    async def get(self, table: str, key: Key) -> Row | None: ...
    async def upsert(self, table: str, key: Key, row: Row) -> None: ...
    async def delete(self, table: str, key: Key) -> bool: ...
    # journal ops (llmbroker_calls) — strictly append-only: no update op exists;
    # quality is its own appended record, metrics derive from the cached tail (Plan 2)
    async def append(self, table: str, row: Row) -> None: ...
    async def recent(self, table: str, limit: int, match: Row | None = None) -> list[Row]: ...
        # newest-first by the calls table's timestamp column (`called_at`;
        # the jsonl store orders by `ts`, Plan 2 step 2.7);
        # match = optional equality filter (the `calls(scope=…)` query API);
        # the rebuild reads unfiltered (learning is global, Plan 2)
    async def purge(self, table: str, before: datetime) -> int: ...   # ignores users by design
    async def aclose(self) -> None: ...
```

Note there is **no state table and no state-specific driver ops**: after Plan 2
shared cooldowns ride on the journal rows, so nothing beyond registry, calls,
disabled, and secrets exists in storage.

There is no create-only op, no partial-update op, and no `DuplicateKeyError`:
model CRUD died in Plan 2 (sync mirrors via upsert/delete), secrets `set` is
an upsert, and a disabled verdict is a whole-row upsert of a two-column row.
The generic layer (not the drivers) owns: lazy
one-time `ensure_schema` gating, JSON⇄dataclass translation,
`KeyError` semantics. A driver method body is the one statement that is
genuinely DB-specific.

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
| `StoreRegistry(driver)` | registry protocol (post-Plan 2) | fetch/upsert/delete — globally scoped, no user parameter; consumers: load + mirror `sync` (nothing else writes) |
| `StoreKnowledge(driver, retention=...)` | knowledge protocol (journal + disabled map) | journal: append/recent/purge (`calls(scope=…)` maps to the `match` filter, the rebuild read passes none); disabled map: fetch/upsert on `llmbroker_disabled` |
| `StoreSecrets(driver)` | `MutableSecretsProtocol` | get/upsert with key `(ref,)` — a flat key-value store; lookups stay exact — the own→shared prefix fallback lives in the broker (Plan 2) |

Semantics carried over exactly: `set_disabled` validates the model
name against the loaded registry (broker layer) and upserts the disabled row;
`record_quality` appends a
self-contained quality record unconditionally (no call-row lookup exists,
Plan 2); retention purge (Plan 2 step 2.7) ignores scope, never touches the
disabled map, and runs inside `StoreKnowledge` on the write-path debounce.

**Port protocols change for the registry and secrets**: Plan 2 already
deleted the model-CRUD verbs and moved the user scope into the ref string;
this plan removes the `user_id` parameters from both protocols (Plan 2 passes
`None` everywhere, so this is a mechanical parameter deletion across the
protocols, the standalone file registry, the env secrets backend, and call
sites in `broker/`/`catalog`). Delete `check_user_id` (`models.py`), the
`require_user_id=` constructor flags, and the `UserIdRequired` exception
along with the last caller. The knowledge protocol stays as Plan 2 left it
(it already removed `StateStoreProtocol` and `state_store=`).

## 3.3 Concrete drivers and facades

- `sqlite/driver.py` — aiosqlite; renders DDL from `spec.TABLES`; keeps `PRAGMA
  user_version` as the marker, the path-keyed `_schema_ready` process cache, and
  the `BEGIN IMMEDIATE` dedicated-connection pattern (`sqlite/schema.py:143-168`)
  — the latter now only serializes concurrent first-run creates across OS
  processes.
- `postgres/driver.py` — asyncpg; the caller owns the pool, `aclose()` is a no-op
  (unchanged contract); version via the `llmbroker_schema_version` row.
- `mongodb/driver.py` — motor; version via the `llmbroker_schema_version`
  document.
- **The Redis backend is deleted entirely** — its only role was the state
  store, which no longer exists (Plan 2 derives shared cooldowns from the
  journal). Remove `src/llmbroker/redis/`, the `redis` optional extra from
  `pyproject.toml`, `fakeredis` from the dev group, and the redis test files.
- `aws/secrets.py`, `vault/secrets.py` — stay single-port SDK glue; drop the
  `user_id` parameter, the `require_user_id` flag, and the
  `llmbroker/users/{user_id}/…` path branching — the ref (scope prefix
  included) is the full secret name.

Facades preserve every public name and constructor signature — e.g.
`src/llmbroker/sqlite/__init__.py`:

```python
class Registry(StoreRegistry):
    def __init__(self, db_path: str | Path) -> None:
        super().__init__(SqliteDriver(db_path))
# likewise Secrets, Knowledge
```

Same pattern for `postgres` (wrapping `asyncpg.Pool`) and `mongodb` (wrapping a
Motor database).

**Stacks are replaced by the single source parameter** (user decision, see
`specs/plans/simplify-rationale.md`). Delete `sqlite.Stack`/`postgres.Stack`/`mongodb.Stack`, the
`BackendStack` protocol, and the `stack=` parameter. Instead, the broker's
first positional argument is the data source:

```python
Broker("config.toml")                    # file registry + state/ sibling + env secrets
Broker("llm.db")                         # sqlite: registry+knowledge+secrets, one file
Broker("postgresql://host/db")           # postgres, one driver shared by all ports
Broker("mongodb://host/db")              # mongodb
Broker("config.toml", secrets=Vault(…))  # explicit overrides still win
```

Dispatch rules are dumb and explicit: `.toml` → file config; `sqlite://` /
`.db` / `.sqlite` → sqlite; `postgresql://` / `mongodb://` → by scheme;
anything else → a clear error naming the accepted forms. The chosen backend
package is imported lazily (a bare `import llmbroker` must never pull in a
driver package); a missing extra produces "pip install llmbroker[postgres]".
Internally: one driver per source, shared by `StoreRegistry`/`StoreSecrets`/
`StoreKnowledge` (~40 lines including dispatch). Explicit `registry=` /
`knowledge=` / `secrets=` kwargs remain for mixed setups and take precedence.

Migrate one DB at a time — sqlite → postgres → mongodb. For each: add the driver,
point the facade at the `Store*` classes, run the full suite, then delete that DB's
superseded `{registry,secrets,telemetry,state_store,schema}.py` modules. No
re-export shims — importers already use the facade names, which survive. The
redis package removal and the stack→source swap land as their own steps after
the three DB migrations.

## 3.4 Tests

The suite is already contract-style (fixtures in `tests/conftest.py:118-252`
parametrize each port over its backends) — keep it that way; never a per-backend
copy of a behavior test. Disposition:

| File | Action |
|---|---|
| `tests/test_registry.py`, `test_telemetry_backends.py`, `test_telemetry.py` | keep as-is (modulo Plan 2's knowledge rename) — port behavior through the facades |
| `tests/test_secrets.py` | user-scope cases become plain prefixed-ref round-trips (the `user_id` parameter is gone); the own→shared fallback is broker-level and covered by Plan 2's tests |
| `tests/test_state_store.py` | delete — the state store no longer exists (shared cooling is covered by Plan 2's journal tests; the mongo legacy-datetime repro moves to the driver conformance suite if the underlying datetime handling survives there) |
| `tests/test_schema_migration.py` | shrink: per driver, (a) fresh-DB create is idempotent and stamps the marker, (b) a wrong marker makes `ensure_schema` raise the actionable error |
| new `tests/test_driver_conformance.py` | one suite parametrized over sqlite(file) / sqlite(:memory:) / postgres / mongodb / inmemory drivers: keyed-op round-trips (get/upsert/delete) over single-column text keys (incl. slash-containing secret refs like `user/42/GROQ_API_KEY`); fetch ordering; journal ops incl. `recent` with and without a `match` filter |
| `tests/test_stack.py` | replace with source-parameter tests: each source form wires the right backend; `.toml` vs `.db` discrimination; unknown source → clear error; missing extra → actionable message; explicit kwargs override the source |
| everything else | unchanged by this plan |

## 3.5 Spec touch-up

`specs/reference/architecture.md`: update the backend table and battery matrix
(sqlite, postgres, mongodb, aws/vault — no redis). In "Where each kind lives",
state that dependency-carrying backends are one storage driver per DB behind
shared port logic, and that a custom backend is either one driver or one full
port. Remove the state-store description (shared cooldowns come from the
journal) and the stack section (source parameter instead). Rewrite the "DB
schema" section: schema created on first use; version mismatch fails fast with
an actionable error; no in-place upgrades; **the table schema is not a public
contract** — hosts may query `llmbroker_calls` directly but at their own risk,
the supported read surface is `snapshot()` (raw per-model facts + metrics).
Remove the
summaries and state tables from the schema list; add `llmbroker_disabled`
(admin verdicts, seeded with model names at `sync`). State the scoping matrix:
registry and learning are global; the scope is an opaque string the broker
turns into a secret-ref prefix (own key, falling back to the shared ref) and
a journal attribution field — storage and protocols have no user concept;
429 cooldowns and dead-key drops follow the key hash, 5xx cooldowns are
global. Specs
state the current rule only — no "previously X, now Y" history.

---

## Step order

1. **3.1–3.2** spec.py + driver protocol + generic ports + in-memory driver (new
   code only — nothing deleted, suite stays green)
2. **3.3** sqlite driver+facade → delete old sqlite modules → postgres → mongodb
   (one DB per step, full suite between)
3. **3.3b** delete the redis package/extra/fakeredis; replace stacks with the
   source parameter
4. **3.4** conformance suite + test consolidation
5. **3.5** spec touch-up

**Final gate:** `invoke pre` + full `python -m pytest` green, zero skips.

# DB schema resilience — columns vs JSON

## Plan sequence — step 1 of 4

> **Prerequisites:** none. **Blocks:** `preset-onboarding-effort.md` and
> `optimizer-learned-profile.md` (both build on the storage shape and the
> `RateLimit` / `LLMConfig.rate_limit` / `LLMState` ⇄ dict artifacts defined
> here).

The four plans form one dependency chain; execute in this order:

1. **`db-schema-resilience.md`** *(this plan)* — storage-shape foundation:
   columns-vs-JSON; defines `RateLimit`, `LLMConfig.rate_limit`, the
   `LLMState` ⇄ dict boundary, and the version-gated `ensure_schema` toolkit.
2. **`preset-onboarding-effort.md`** — curated catalog knowledge, effort/value
   onboarding, warm-start seeding, the `EXHAUSTED` phase, and the
   keyless-not-routable pool change.
3. **`optimizer-learned-profile.md`** — the durable learned half (learned profile
   carried in the registry, bench verdict) and `SeedPolicy.SYNC`; extends the
   routable predicate from (2).
4. **`catalog-refresh.md`** — the manual re-curation runbook; consumes the
   taxonomies fixed in (2) and may run in parallel with (3).

## Problem statement

llmbroker persists four kinds of data across four backends (sqlite, postgres,
mongodb, redis). Some of that data is stored as **typed columns** whose shape
churns as features grow — e.g. per-LLM runtime state (`phase`, `cooldown_until`,
`fail_count`) gains fields as the optimizer/FSM evolves, and each addition is a
coordinated schema change across the SQL backends for data no one ever queries by
those fields. That is pure overhead: the migration machinery exists, but spending
it on never-queried, high-churn payload buys nothing.

This plan makes the schema resilient to that churn without sacrificing the query
performance that the parts we *do* search depend on. It is a prerequisite for
[`preset-onboarding-effort.md`](preset-onboarding-effort.md), which adds new
per-LLM state (an `EXHAUSTED` phase) and new per-model config (`rate_limit`) that
would otherwise each force a migration.

## Decision: columns for what you query, one JSON blob for the rest

The rule is narrow and mechanical:

> A field earns a dedicated column only if it appears (or realistically will) in
> a `WHERE` / `JOIN` / `ORDER BY` / `GROUP BY` / aggregate. Everything else is
> payload and lives in a **single JSON column** keyed by the row's identity.

This is a **hybrid**, not "JSON everywhere". The identity/scoping/queried fields
stay first-class columns (and keep their indexes); only the open-ended,
never-queried, or nested slice moves to JSON.

### Is JSON actually the right serialization?

Alternatives considered for the payload slice, and why JSON wins here:

- **Add-a-nullable-column each time** — correct for *stable, queryable* data
  (that is exactly what telemetry does), but wasteful for volatile state we never
  query: every optimizer tweak would touch two SQL schemas for nothing. Also
  nested values (e.g. `rate_limit` = rpm/rpd/tpm/tpd) do not map to a single
  scalar column, forcing either four columns or a blob anyway.
- **EAV (attribute rows)** — a well-known anti-pattern: kills readability, needs
  joins/pivots, and coerces types poorly. Rejected.
- **Binary (MessagePack/pickle)** — marginally smaller than JSON, but opaque to
  SQL tooling, not human-inspectable, and forfeits the DB's native JSON
  indexing. State is small and ephemeral, so the size win is irrelevant.
  Rejected.
- **JSON / JSONB** — human-readable, inspectable via SQL, and *not* a query
  dead-end: postgres `JSONB` supports GIN/expression indexes and sqlite has
  `json_extract` you can index. So if a payload field unexpectedly becomes a
  filter, you can index into the blob without a backfill — you just don't design
  hot paths around it. Redis and mongodb are already document/JSON stores.

Conclusion: **JSON (JSONB on postgres, TEXT+json1 on sqlite, native on
mongo/redis) is the logical choice for the payload slice.** Keep one typed
dataclass ⇄ dict boundary in code so the blob still has a schema, just not in the
DB.

### Per-table verdict (from the actual query patterns)

| Table | Identity / queried columns (stay) | Payload → JSON | Verdict |
|---|---|---|---|
| `llmbroker_calls` (telemetry) | `id`, `llm_name`, `called_at`, `user_id`, `status` — used in `WHERE`/`GROUP BY`/`ORDER BY`/`DELETE`, indexed | already `usage_extra` | **Unchanged.** This is the searchable log; its fields are the query surface. It already models the rule perfectly (known token counts = columns, open-ended provider extras = `usage_extra` JSON). |
| `llmbroker_state` | `llm_name`, `user_id` | `phase`, `cooldown_until`, `fail_count`, future FSM/optimizer fields | **→ JSON body.** Never queried by inner field; whole-row R/W by key; high churn; ephemeral. |
| `llmbroker_registry` | `name`, `user_id` (the only things queried: `WHERE name=?`, `ORDER BY name`, unique index) | nested/open config (`rate_limit`, future per-LLM knobs) | **Hybrid.** Keep the core scalar columns (`base_url`, `model`, `api_key_ref`) — stable, human-meaningful, durable — and add **one** JSON column for nested/open-ended config. Not a full blob: durable data stays transparent. |
| `llmbroker_secrets` | `ref`, `user_id` | `value` (single opaque scalar) | **Unchanged.** JSON buys nothing for one scalar with no sub-structure. |

## Implementation

Ordered steps. Run `invoke pre` and `python -m pytest` after each; both green,
no skips (testcontainers cover postgres/mongo, `fakeredis` covers redis).

### Step 1 — `LLMState` ⇄ dict boundary

File: `src/llmbroker/models.py`.

- Add an `extra: dict[str, object] = field(default_factory=dict)` field to
  `LLMState` — same purpose as `Usage.extra` already in this file (the landing
  spot for an unknown/future key so it survives a round-trip), though not the
  same signature: `Usage.extra` is `dict[str, int] | None = None`, while
  `LLMState.extra` needs a non-nullable factory default since it is always
  merged into unconditionally. `LLMState` stays `frozen, slots=True`: a
  declared field works fine under `slots`, and dropping `slots` on a
  four-field dataclass would buy nothing.
- Add `LLMState.to_dict() -> dict` and `LLMState.from_dict(d: dict) -> LLMState`.
  `to_dict` serializes `phase.value`, `cooldown_until` (ISO-8601 or `None`),
  `fail_count`, then merges in `extra`'s keys at the top level — but must
  raise if `extra` contains `phase`/`cooldown_until`/`fail_count` rather than
  silently letting it clobber the known fields. `from_dict` can never produce
  such an `extra` (it always scoops those three names out before collecting
  the rest), so the only way to hit this is a hand-built
  `LLMState(extra={"phase": ...})`, but the guard keeps that failure loud
  instead of a silently corrupted round-trip. `from_dict`
  reads `phase`/`cooldown_until`/`fail_count` **via `.get()` with the same
  defaults the dataclass fields already have** (not direct indexing) and
  collects every other key into `extra` — a document missing a field must
  fall back to the default, not raise `KeyError`. This matters beyond
  future-proofing: Step 2's redis/mongo stores currently write an ad-hoc
  subset of these keys (e.g. `cooldown_until` only when set), so during a
  rolling upgrade `from_dict` will see old-shape documents with missing keys
  before every writer has switched to `to_dict()`. Doctest the round-trip.
- Add a shared `reconcile(state: LLMState, now: datetime) -> LLMState`
  function next to `LLMState` (or a method on it). It implements the
  trust-stored-phase / expired-cooldown rule that today is hand-copied,
  identically, in all four state stores' `read()` (`stored_phase in
  {OFFLINE, PROBING}` ⇒ trust it and clear an expired cooldown; else an
  unexpired cooldown ⇒ `COOLING`; else ⇒ `AVAILABLE`; anything else raises).
  Since Step 2 already touches every one of those four files to switch them
  onto `from_dict`, this is the point to delete the 4x duplication rather
  than re-paste the same block a fifth time (SQL backends currently have it
  copied in Python after manual column reads; Step 2 would otherwise
  re-copy it again after `json.loads`). Each backend becomes
  `reconcile(LLMState.from_dict(json.loads(...)), now)`.

Tests (`tests/test_state.py` — it already imports `LifecyclePhase`/`LLMState`
from `llmbroker.models`, so new model-level tests join it rather than
starting a fresh `tests/test_models.py`): round-trip incl. tz-aware
`cooldown_until`, a `None` cooldown, and an unknown/extra dict key preserved
via the new `extra` field; the `extra`-collides-with-a-reserved-key case
raises; `reconcile()` covering trusted-phase-with-expired-cooldown,
trusted-phase-with-live-cooldown, untrusted-phase-with-live-cooldown ⇒
`COOLING`, and the unexpected-phase `ValueError`.

### Step 2 — state stores persist a JSON document

Files: `src/llmbroker/protocols/state_store.py`, the four state stores
(`sqlite`, `postgres`, `mongodb`, `redis`), `sqlite/schema.py`,
`postgres/schema.py`.

- Keep the protocol shape (`read() -> dict[str, LLMState]`, `write(name, state)`).
  Internally persist each `(llm_name, user_id)` as one JSON document from
  `LLMState.to_dict()`.
- SQL: replace the `phase` / `cooldown_until` / `fail_count` columns with a single
  `state` column — `JSONB` on postgres, `TEXT` on sqlite. Keep `llm_name`,
  `user_id`, and the unique index on `(llm_name, COALESCE(user_id))`. On read,
  call `reconcile(LLMState.from_dict(json.loads(...)), now)` — the shared
  helper from Step 1, not a re-copy of the trust/expiry rule.
- Redis already stores a JSON string per name in a hash, and mongo stores a
  document — switch both to the full `to_dict()` payload (drop the ad-hoc field
  handling) and likewise call the shared `reconcile()` on read instead of their
  own copies of the same rule.
- Bump `sqlite/_SCHEMA_VERSION` (and the postgres equivalent). State is a live
  cache rebuilt from traffic, so dropping existing state rows on upgrade is
  acceptable — **no data migration**.
- **`ensure_schema` currently has no upgrade path for an already-provisioned
  database, and today reads the version marker only *after* creating tables
  (sqlite) or never compares it at all (postgres).**
  `sqlite/schema.py::ensure_schema` runs `CREATE TABLE IF NOT EXISTS` for every
  table first, then reads `PRAGMA user_version` and bumps it — so inserting a
  migration branch means splitting the function into three phases, not just
  adding a line: (1) read the version marker; (2) if it is stale, run
  **drop-based** migrations — `DROP TABLE IF EXISTS llmbroker_state` — while
  the old shape still exists (or against nothing at all on a brand-new DB,
  where the drop is a harmless no-op); (3) run `CREATE TABLE IF NOT EXISTS`
  for every table unconditionally, which now guarantees every table exists,
  in the new shape for a freshly-created `llmbroker_state` and unchanged for
  a pre-existing `llmbroker_registry`; then bump the marker.
  **Additive `ADD COLUMN` migrations (Step 3) must run *after* phase (3),
  never before** — see Step 3 for why, and for how this differs from the
  drop above.
  `postgres/schema.py::ensure_schema` is a bigger gap than "equivalent to
  sqlite" implies: it doesn't read `llmbroker_schema_version` at all today —
  it unconditionally runs the DDL block and unconditionally upserts
  `_SCHEMA_VERSION` on the pool's first call. This step has to *add* the
  read-then-compare step from scratch (`SELECT version FROM
  llmbroker_schema_version`, treating a missing row as version 0), not just
  bolt a migration branch onto existing gating, and the same three-phase split
  (pre-create drop, unconditional create, post-create add-column) applies.
  Since state is disposable, the drop-based migration itself is a
  version-gated `DROP TABLE IF EXISTS llmbroker_state` (sqlite) / equivalent
  drop (postgres) executed when the stored version marker (`PRAGMA
  user_version` / `llmbroker_schema_version`) is below `_SCHEMA_VERSION` — so
  the table is actually recreated in the new shape instead of silently left
  in the old one.
- **Concurrency: guard the read-marker → migrate → bump-marker sequence with
  a real cross-process lock, not an in-process `asyncio.Lock`.**
  `sqlite/state_store.py`'s docstring says this backend is for "multiple
  workers... on a single machine" sharing one file — i.e. multiple *OS
  processes*, not just concurrent tasks in one process. An `asyncio.Lock()`
  (the mechanism `postgres/schema.py`'s `_schema_lock` uses) only serializes
  callers within a single process; each worker process gets its own Lock
  instance, so two processes can still both observe the same stale
  `PRAGMA user_version` before either commits the bump. For this step's
  `DROP TABLE IF EXISTS` that race is harmless (idempotent), but Step 3's
  sqlite `ALTER TABLE ... ADD COLUMN` has no `IF NOT EXISTS` form and raises
  `OperationalError: duplicate column name` if two processes both run it —
  exactly the failure a lock is meant to prevent, so an in-process lock alone
  does not actually fix it.
  Instead, wrap the whole read-marker → migrate → bump-marker sequence in a
  single `BEGIN IMMEDIATE` transaction. sqlite grants the connection a
  RESERVED file lock for the transaction's duration, which blocks every other
  connection (any process) from writing until it commits — a guarantee an
  in-memory lock cannot give across processes. Keep the process-local
  `_schema_ready` dict as a fast-path short-circuit so a process that has
  already seen the current version skips opening the transaction at all; it
  is an optimization, not the correctness mechanism — the `BEGIN IMMEDIATE`
  transaction is.
  **Implementation note:** `aiosqlite` (like stdlib `sqlite3`) defaults to
  `isolation_level=""`, under which the driver opens its own implicit `BEGIN`
  before DML and manages commits itself — an explicit `BEGIN IMMEDIATE` sent
  through `execute()` on such a connection does not reliably grant the
  RESERVED lock described above. Open the connection (or this specific
  operation) with `isolation_level=None` (autocommit) so the driver steps
  aside and the explicit `BEGIN IMMEDIATE` / `COMMIT` pair is what actually
  executes; verify this against the installed `aiosqlite` version rather than
  assuming it, since silently getting a weaker lock than intended would defeat
  the point of this change.
  Postgres does not need an equivalent lock rewrite: `CREATE TABLE` /
  `ALTER TABLE` already take an `ACCESS EXCLUSIVE` lock on the table for the
  statement's duration, so two processes racing `ensure_schema` serialize at
  the database level regardless of the in-process `asyncio.Lock` (which only
  ever protected against races within one process). The read-then-compare
  logic being added to `postgres/schema.py::ensure_schema` above can stay
  inside the existing `conn.transaction()` block; no separate locking
  mechanism is needed there.

Tests: each backend round-trips an `LLMState` through `write`/`read`, including a
future-proofing extra key; expired `cooldown_until` reads back `AVAILABLE`;
`OFFLINE`/`PROBING` survive. Add a migration test: seed a sqlite/postgres DB
with the old `llmbroker_state` shape (old version marker), run `ensure_schema`,
assert the table now has the `state` column and the version marker is current.

### Step 3 — registry stores nested/open config as JSON

Files: `src/llmbroker/standalone/registry.py`, `src/llmbroker/protocols/registry.py`
(if the contract needs it), the DB registries (`sqlite`, `postgres`, `mongodb`),
`sqlite/schema.py`, `postgres/schema.py`, `src/llmbroker/models.py`.

- Add a JSON column to the registry row — `metadata` (`JSONB`/`TEXT`) — for
  nested/open-ended per-LLM config. Keep `name`, `base_url`, `model`,
  `api_key_ref`, `user_id` as columns (identity stays queryable; core config
  stays transparent). Additive column ⇒ bump the schema version; existing rows
  read back with an empty/`NULL` metadata.
- **Unlike state, registry rows are durable and must not be dropped**, so this
  column cannot rely on the `CREATE TABLE IF NOT EXISTS` in `ensure_schema` —
  that statement is a no-op against a table that already exists, so an
  already-provisioned `llmbroker_registry` would never actually gain the
  `metadata` column. Add an explicit, version-gated
  `ALTER TABLE llmbroker_registry ADD COLUMN metadata TEXT` (sqlite) /
  `ADD COLUMN IF NOT EXISTS metadata JSONB` (postgres) in `ensure_schema`,
  run when the stored version marker is below the new `_SCHEMA_VERSION`, so
  existing rows keep their data and gain `metadata = NULL`.
  **This must run *after* phase (3) from Step 2 (`CREATE TABLE IF NOT EXISTS`
  for every table), never before** — on a brand-new database
  `llmbroker_registry` does not exist until that `CREATE TABLE` runs, and
  `ALTER TABLE` on a nonexistent table fails (`no such table` on sqlite,
  `relation does not exist` on postgres) regardless of `IF NOT EXISTS` on the
  column. This is the opposite ordering from the state drop in Step 2, which
  must run *before* the `CREATE TABLE IF NOT EXISTS` block — the two
  migrations are not interchangeable in position, and "run every pending
  migration" must not be read as one undifferentiated batch that can go
  either side of phase (3).
  sqlite's `ALTER TABLE ... ADD COLUMN` has no `IF NOT EXISTS` form and errors
  on a repeat run — this must execute inside the same `BEGIN IMMEDIATE`
  transaction added in Step 2, not just behind the version check, since the
  version check alone does not stop two concurrent processes from both
  passing it before either commits the marker bump. As a defense-in-depth
  backstop (not a replacement for the version gate), check
  `PRAGMA table_info(llmbroker_registry)` for an existing `metadata` column
  before issuing the `ALTER` on sqlite, so a stray second pass can't crash the
  process; postgres's `IF NOT EXISTS` already gives this for free.
- **Version sequencing:** Step 2 and Step 3 each introduce their own
  migration (state-drop, registry-`ALTER`) and, taken together, `_SCHEMA_VERSION`
  moves from its pre-plan value to one new value covering both — whether that
  is one bump or two intermediate ones depends only on whether the two steps
  ship in the same change or land separately; either way, a database
  upgrading directly from the pre-plan version must run *both* migrations in
  one pass, and there is no need to track intermediate version numbers for
  that. But the two migrations are **not** interchangeable in position: the
  gate is not "run every pending migration step" as one undifferentiated
  batch. One `ensure_schema` pass, once the marker is found stale, is:
  read marker → drop-based migrations (state) → `CREATE TABLE IF NOT EXISTS`
  for every table → additive `ADD COLUMN` migrations (registry `metadata`),
  guarded by the column-existence check from Step 3 → bump marker. Placing
  the `ADD COLUMN` step before the `CREATE TABLE IF NOT EXISTS` block breaks
  the very first `ensure_schema` call against a brand-new database, since
  `llmbroker_registry` does not exist yet at that point.
- `LLMConfig` carries the structured optional config that serializes into
  `metadata`. Its first field is `rate_limit` (a nested rpm/rpd/tpm/tpd value);
  the `RateLimit` type and `LLMConfig.rate_limit` field are **defined here** so
  registries can round-trip them, and are *populated/consumed* by the onboarding
  plan. `LLMConfig` is a frozen dataclass whose four existing fields (`name`,
  `base_url`, `model`, `api_key_ref`) are constructed positionally-by-keyword
  all over the codebase (every DB registry, the standalone registry); add
  `rate_limit: RateLimit | None = None` as a fifth, defaulted field so those
  call sites keep working unchanged.
- Each DB registry writes `LLMConfig`'s structured part into `metadata` and reads
  it back on `load`/`get`. **`update()` must also write `metadata`**, but the
  fix differs by backend: `sqlite`/`postgres` `registry.py` `update()` runs an
  `UPDATE ... SET base_url=?, model=?, api_key_ref=?` with an explicit column
  list — extend that list to include `metadata`, otherwise the update silently
  drops previously-stored `rate_limit`/config. `mongodb/registry.py`
  `update()` already does a full-document `replace_one`, not a partial
  `UPDATE`, so there's no column list to extend — it just needs `metadata`
  added to the `doc` dict it builds, the same one-line addition `add()` needs.
  The file (TOML) registry reads `rate_limit` from the `[[llms]]` row (see
  onboarding plan) — it has no DB to migrate.

Tests (`tests/test_registry.py`, parametrized across backends via the
`mutable_registry` fixture): a DB registry round-trips an `LLMConfig` whose
`rate_limit` is set and whose `rate_limit` is `None`; a pre-existing row with
`NULL` metadata loads as `rate_limit=None`; core columns still queried by name;
`update()` preserves/overwrites `metadata` (round-trips a changed `rate_limit`).
Add a migration test: seed a sqlite/postgres `llmbroker_registry` row in the old
shape (no `metadata` column, old version marker), run `ensure_schema`, assert
the column now exists and the pre-existing row is intact with `metadata` `NULL`.

> **Forward note.** [`optimizer-learned-profile.md`](optimizer-learned-profile.md)
> adds a *second* JSON column — `profile` — on this same `llmbroker_registry` row
> for the durable learned half (quality aggregate + bench verdict), reusing this
> step's version-gated additive `ALTER` path. The two are deliberately distinct:
> `metadata` is curated static config that `apply_seed` overwrites from the
> preset; `profile` is optimizer-owned and the seed **never** writes it. There is
> **no** separate profile store — learned data is another field of the registry
> row, keyed by the same `(name, user_id)`.

## Non-goals

- **Touching telemetry's columnar schema** — it is the query surface; its fields
  earn their columns. Its `usage_extra` JSON already handles the open-ended part.
- **Full-blobbing the registry** — durable, human-meaningful core config
  (`base_url`, `model`, `api_key_ref`) stays in transparent columns; only
  nested/open-ended knobs go to JSON.
- **A data migration for state** — state is ephemeral and rebuilt from traffic.
- **Persisting the optimizer's learned profile** — out of scope here; this plan
  only makes the *shape* free to extend. The durable learned half (quality
  aggregate + bench verdict) is added by
  [`optimizer-learned-profile.md`](optimizer-learned-profile.md) as a second JSON
  column on the registry row (distinct from `metadata`), not a new backend;
  ephemeral optimizer hints, if ever persisted, ride in the state document.

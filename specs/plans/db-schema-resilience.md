# DB schema resilience — columns vs JSON

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

- Add `LLMState.to_dict() -> dict` and `LLMState.from_dict(d: dict) -> LLMState`.
  Serialize `phase.value`, `cooldown_until` (ISO-8601 or `None`), `fail_count`.
  Write it generically so any later `LifecyclePhase` value or extra key
  round-trips without change. Doctest the round-trip.

Tests (`tests/test_models.py`): round-trip incl. tz-aware `cooldown_until`, a
`None` cooldown, and an unknown/extra dict key preserved.

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
  `LLMState.from_dict(json.loads(...))` and apply today's reconciliation
  (expired `cooldown_until` ⇒ `AVAILABLE`; trust stored `OFFLINE`/`PROBING`) off
  the parsed dict.
- Redis already stores a JSON string per name in a hash, and mongo stores a
  document — switch both to the full `to_dict()` payload (drop the ad-hoc field
  handling).
- Bump `sqlite/_SCHEMA_VERSION` (and the postgres equivalent). State is a live
  cache rebuilt from traffic, so dropping existing state rows on upgrade is
  acceptable — **no data migration**.
- **`ensure_schema` currently has no upgrade path for an already-provisioned
  database.** `CREATE TABLE IF NOT EXISTS` is a no-op against a table that
  already exists in the old shape (`phase`/`cooldown_until`/`fail_count`
  columns) — bumping the version constant alone does not touch it, and the new
  code reading/writing a `state` column would then fail against the stale
  schema. Since state is disposable, the fix is a version-gated
  `DROP TABLE IF EXISTS llmbroker_state` (sqlite) / equivalent drop (postgres)
  executed when the stored version marker (`PRAGMA user_version` /
  `llmbroker_schema_version`) is below `_SCHEMA_VERSION`, before the
  `CREATE TABLE IF NOT EXISTS` runs — so the table is actually recreated in the
  new shape instead of silently left in the old one.

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
- `LLMConfig` carries the structured optional config that serializes into
  `metadata`. Its first field is `rate_limit` (a nested rpm/rpd/tpm/tpd value);
  the `RateLimit` type and `LLMConfig.rate_limit` field are **defined here** so
  registries can round-trip them, and are *populated/consumed* by the onboarding
  plan.
- Each DB registry writes `LLMConfig`'s structured part into `metadata` and reads
  it back on `load`/`get`. **`update()` must also write `metadata`** — today's
  `sqlite/registry.py` `update()` only sets `base_url`/`model`/`api_key_ref`;
  extend its `UPDATE` statement to include `metadata` (same for the postgres/
  mongo registries), otherwise an update silently drops previously-stored
  `rate_limit`/config. The file (TOML) registry reads `rate_limit` from the
  `[[llms]]` row (see onboarding plan) — it has no DB to migrate.

Tests (`tests/test_registry_*`): a DB registry round-trips an `LLMConfig` whose
`rate_limit` is set and whose `rate_limit` is `None`; a pre-existing row with
`NULL` metadata loads as `rate_limit=None`; core columns still queried by name;
`update()` preserves/overwrites `metadata` (round-trips a changed `rate_limit`).
Add a migration test: seed a sqlite/postgres `llmbroker_registry` row in the old
shape (no `metadata` column, old version marker), run `ensure_schema`, assert
the column now exists and the pre-existing row is intact with `metadata` `NULL`.

## Non-goals

- **Touching telemetry's columnar schema** — it is the query surface; its fields
  earn their columns. Its `usage_extra` JSON already handles the open-ended part.
- **Full-blobbing the registry** — durable, human-meaningful core config
  (`base_url`, `model`, `api_key_ref`) stays in transparent columns; only
  nested/open-ended knobs go to JSON.
- **A data migration for state** — state is ephemeral and rebuilt from traffic.
- **Persisting the optimizer's live in-memory internals** — out of scope here;
  this plan only makes the *shape* free to extend. If/when those hints are
  persisted, they ride in the state document.

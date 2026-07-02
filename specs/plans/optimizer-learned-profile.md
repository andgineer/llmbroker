# Learned LLM profile — the dynamic, optimizer-owned half of the catalog

## Plan sequence — step 2 of 3

> **Prerequisites:** the typed dataclass ⇄ JSON-document boundary and the
> durable version-gated `ensure_schema` path this plan reuses for the new
> `profile` column are already implemented (see
> [`architecture.md`](../reference/architecture.md#columns-vs-json)) — **and**
> `preset-onboarding-effort.md` (step 1) — this plan extends its
> keyless-not-routable pool change and its zero-routable alarm
> (`routable ⟺ keyed **and** not benched`). Do the onboarding pool change first.
> **Blocks:** nothing.

The remaining three plans form one dependency chain; execute in this order:

1. **`preset-onboarding-effort.md`** — curated catalog knowledge, effort/value
   onboarding, a simplified two-phase (AVAILABLE/COOLING) reliability model,
   and the keyless-not-routable pool change.
2. **`optimizer-learned-profile.md`** *(this plan)* — the durable learned half
   (learned profile carried in the registry, bench verdict) and
   `SeedPolicy.SYNC`; extends the routable predicate from (1).
3. **`catalog-refresh.md`** — the manual re-curation runbook; consumes the
   taxonomies fixed in (1) and may run in parallel with this plan.

## Problem statement

llmbroker's concept is shifting: the LLM list is **curated by the maintainers**
and shipped as a preset; the end user mostly just supplies keys and does not
care which models are inside — they want it to work and not be nagged for new
keys. Under that concept the persisted data splits cleanly by *owner* and
*durability*, and today one quadrant has no home:

| Layer | Written by | Durability |
|---|---|---|
| **Curated catalog** (`llmbroker_registry` static columns + `metadata`) | preset roll / admin only | durable |
| **Ephemeral sync-state** (`llmbroker_state`) | the running broker, to coordinate instances | ephemeral — rebuilt from traffic, dropped on schema bump |
| **Raw call log** (`llmbroker_calls`) | the running broker | durable, but append-only |
| **Learned per-LLM knowledge** | the optimizer (+ admin overrides) | **durable — missing today** |

The missing quadrant is the **only user-side data that is genuinely valuable and
not cheaply recoverable**: how useful a given model has proved to be. A blocked
key recovers on its own; a model that stopped working sits idle and harms
nothing; but the accumulated judgement "this model is/ isn't worth routing to"
is earned from real traffic and user ratings and must survive both a **preset
update** and an **ephemeral-state reset**.

Two concrete gaps follow:

1. **The quality aggregate is not durable.** `Optimizer.usable_rate` is computed
   from an in-memory rolling window that is lost on restart, and the durable
   `quality_score` ratings in `llmbroker_calls` are never aggregated back on
   load. A restarted process re-learns each model's usefulness from zero. The
   raw call log is the wrong place to *read* a per-LLM property from — in the
   simplest (JSONL) deployment that means scanning the whole file.
2. **There is no way to permanently bench a globally useless model.** The
   optimizer's quality floor is a *soft, per-request* routing gate: a bad model
   is merely deprioritised and still used as a last-resort fallback, and it is
   re-seeded fresh on the next preset roll. There is no persistent "this model is
   too low-quality for **all** tasks — keep its stats but never route to it, and
   do not let a catalog update resurrect it."

This plan adds the missing durable, optimizer-owned layer — the **learned
profile** — and the reconcile behaviour that lets the curated catalog *flow* to
users without ever clobbering it.

## Concept: the catalog has two halves, one store

Logically the LLM catalog is one thing, but it has a **static half** and a
**dynamic half**. They must be *field-separated* so a catalog refresh can
overwrite one without touching the other — but they do **not** need separate
backends. Both live in the **registry**, keyed by the same `(name, user_id)`:

- **Static half — the curated catalog**: `name`, `base_url`, `model`,
  `api_key_ref`, and curated per-model config (`rate_limit`, …) in `metadata`.
  Owned by the preset / admin. The optimizer never writes here. Freely
  overwritten by a preset roll.
- **Dynamic half — the learned profile**: what *this deployment* has learned
  about each model. Owned by the optimizer (with manual admin overrides). Stored
  in the registry's own `profile` field, which the seed **never** writes.

There is deliberately **no separate profile backend**. Adding a fifth store type
(protocol + four DB implementations + a standalone file) for data that is
per-model and lives and dies with the registry row is overhead the package does
not need. The learned profile is another field of the registry, and the
"never clobber learned data on re-seed" guarantee is a narrow, testable rule —
*the seed writes only static fields* — not a whole parallel store.

The learned profile carries two things, both genuinely per-model and both more
than a lone flag:

- the **quality aggregate** — a compact, durable summary of the model's rated
  usefulness (the persisted form of `usable_rate`), so warm-start survives
  restart and does not require scanning the call log;
- the **bench verdict** — `benched` + `source (auto|manual)` + `since`, the
  standing decision to exclude a model from routing while keeping its stats.

A "benched" model is deliberately distinct from the ephemeral lifecycle
`phase` (`AVAILABLE/COOLING` — see
[`preset-onboarding-effort.md`](preset-onboarding-effort.md) for why the phase
model stays this small): `phase` is transient and lives in the disposable
sync-state; `benched` is a durable verdict, orthogonal to it.

## The learned profile lives in the registry

Rather than a parallel store, the registry gains the ability to read and write a
per-`(name, user_id)` learned profile alongside the config it already serves.

- **`LLMProfile`** (`src/llmbroker/models.py`, one typed dataclass ⇄ dict
  boundary, same pattern as `LLMState`): a per-operation quality summary plus the
  bench verdict fields. Serialises to a single JSON document — reuse the
  columns-vs-JSON decision already implemented (see
  [`architecture.md`](../reference/architecture.md#columns-vs-json)): `name`,
  `user_id` are already the registry row's identity columns; the profile is one
  JSON body.
- **Registry protocol** (`src/llmbroker/protocols/registry.py`): add
  `read_profiles(user_id) -> dict[str, LLMProfile]` and
  `write_profile(name, profile, user_id) -> None`. No new protocol file, no new
  backend — these are two more methods on the registry every backend already
  implements.
- **DB registries** (`sqlite`, `postgres`, `mongodb`): add **one** JSON
  column `profile` (`JSONB`/`TEXT`) on the existing `llmbroker_registry` row,
  next to `metadata`. Use the same **durable** version-gated `ensure_schema`
  path already in place for `metadata` (additive `ALTER`; never `DROP` — the
  row is the valuable data). `read_profiles` selects `(name, profile)`;
  `write_profile` updates only the `profile` column. Mongo stores the
  `to_dict()` document in the same record under a `profile` key.
- **File/TOML registry** (`src/llmbroker/standalone/registry.py`): today
  read-only. Give it a **write path for the profile only** — the config stays
  read from `llm.toml`/`.json`, the learned profile is persisted to a **sibling
  JSON file**:
  - default path: `<config_stem>.profile.json` next to the config file
    (e.g. `llm.toml` → `llm.profile.json`);
  - overridable in `Registry.__init__(path, *, profile_path=None,
    persist_profile=True)`;
  - `persist_profile=False` (or `profile_path` unset with persistence disabled)
    ⇒ profiles live only in memory for the process, writes are no-ops, and
    `read_profiles` returns `{}`. This is the explicit "do not write any profile
    file" mode for read-only / zero-write deployments.
  - Keeping the profile in a **separate sibling file** (not inside `llm.toml`) is
    what makes a preset overwriting `llm.toml` physically unable to touch learned
    data — the file registry's version of the "seed never writes profile" rule.
- **Default is in-memory.** When a deployment does not configure profile
  persistence (DB registry always persists; file registry with
  `persist_profile=False`), learned data and manual bench last only for the
  process — an accepted trade-off for zero-config use, exactly like the state
  store.

## Bench verdict

`benched` is the one situation where the optimizer *or* the user removes a model
from the pool while keeping its statistics. `source` records who benched it,
because the two behave differently on recovery:

### Auto (optimizer)

The optimizer benches a model only when it is too low-quality for **all** tasks,
not merely for some:

- quality is judged from the user's **`quality_score` ratings**, aggregated per
  `(name, operation)` — the genuine usefulness signal — not from call `status`
  (which only reflects transport success);
- a model is a candidate for auto-bench when **every** operation that has at
  least `min_rated_samples` ratings sits below a `bench_floor`, i.e. there is no
  task it is still good enough for (this is what distinguishes it from a model
  that is weak on hard tasks but fine on simple ones, which the existing
  per-operation soft floor already handles);
- **hysteresis** avoids flapping: bench on/off use a margin (bench below
  `bench_floor`, un-bench only once some operation climbs back above
  `bench_floor + bench_margin`), and require sustained evidence over the rating
  window;
- an auto-bench **lifts automatically** if ratings recover (the aggregate is
  durable, so recovery is observed across restarts too);
- with no ratings at all, a model is **never** auto-benched — auto-bench is
  conservative and refuses to judge without evidence.

This is a stronger, persistent, pool-level exclusion that composes with — does
not replace — the existing soft per-request quality floor in `OptimizerPolicy`:
the soft floor ranks/gates among *routable* candidates; a bench removes the model
from the routable set entirely and persists that decision.

### Manual (admin)

`disable_llm(name, reason=…)` / `enable_llm(name)` on the broker set/clear a
`source=manual` bench. A manual bench **sticks** until manually cleared —
recovering ratings do not lift it, and a preset roll does not clear it. A manual
`enable_llm` also clears an auto-bench and suppresses immediate re-bench for the
rating window (an explicit human "I know, use it anyway").

### Pool overlay

A benched model stays visible in `configs` (snapshots, `env`, stats) but is
**not routable**: its slot is never enqueued. This is the same
"present-but-not-routable" mechanic that
[`preset-onboarding-effort.md`](preset-onboarding-effort.md) introduces for
keyless configs, so the routable predicate becomes:

> a config is routable ⟺ it has a resolved key **and** it is not benched.

The genuine `AllLLMsFailedError` / underprovision alarm fires only when **zero
routable** configs remain (already the onboarding plan's rule) — benching the
last usable model surfaces as that same zero-routable alarm.

## Re-seed: the catalog flows to users (SeedPolicy.SYNC)

Today the default `SeedPolicy.IF_EMPTY` seeds a user once and never delivers
later catalog updates — the opposite of "maintainers curate, updates reach
users". Add a new policy and make it the default:

- **`SeedPolicy.SYNC`** (new default): on every provision, **upsert** the curated
  catalog — `add` models new in the preset, `update` the static config of models
  already present — but **do not remove** models that dropped out of the preset.
  A dropped model simply sits idle; dead models harm nothing, and removing it
  would throw away the user's learned profile for it.
- `apply_seed` writes **only the static fields** of the registry row
  (`base_url`, `model`, `api_key_ref`, `metadata`) — never the `profile` column /
  sibling file. That single rule is the "never clobber learned data" guarantee:
  learned quality and bench verdict are structurally out of the seed's write set.
- `MIRROR` (removes dropped models) and `ADD` / `IF_EMPTY` remain available for
  callers who want them; only the default changes.

Note `SYNC`'s `update` overwrites the **static** registry config (e.g. a revised
`rate_limit` in `metadata`) from the preset — which is correct, that is the
curated half doing its job — while the `profile` column's quality aggregate and
bench verdict are untouched.

## Relationship to the other plans

- The storage-shape foundation (see
  [`architecture.md`](../reference/architecture.md#columns-vs-json)): reuse its
  typed dataclass ⇄ JSON-document boundary and its version-gated `ensure_schema`
  pattern for the new `profile` column. That column uses the **durable**
  (`ALTER`, keep rows) path, contrasting the state store's disposable (`DROP`)
  path. No new decision, no new backend — same toolkit, one more registry
  field. The registry's `profile` column round-trips independently of
  `LLMState`/`reconcile()`, so it is unaffected by whatever shape `phase` takes.
- [`preset-onboarding-effort.md`](preset-onboarding-effort.md): this plan
  composes with its keyless-not-routable pool change (routable ⟺ keyed **and**
  not benched) and its zero-routable alarm. Do the onboarding pool change first;
  this plan extends the same predicate. `phase` stays limited to
  `AVAILABLE`/`COOLING`; `benched` never becomes a `LifecyclePhase` value.

## Implementation

Ordered, self-contained steps. Run `invoke pre` and `python -m pytest` after
each; both green, no skips (testcontainers cover postgres/mongo, `fakeredis`
covers redis).

### Step 1 — `LLMProfile` model + `BenchSource` enum

File: `src/llmbroker/models.py`.

- Add `class BenchSource(Enum)`: `AUTO = "auto"`, `MANUAL = "manual"`.
- Add `@dataclass class LLMProfile` with: a per-operation quality summary
  (e.g. `quality: dict[str | None, QualitySummary]` where `QualitySummary`
  carries `rated_count` and `good_count`, sufficient to reconstruct a
  Laplace-smoothed rate), `benched: bool = False`,
  `benched_source: BenchSource | None = None`,
  `benched_since: datetime | None = None`, `benched_reason: str | None = None`.
- Add `LLMProfile.to_dict()` / `from_dict()` written generically so future keys
  round-trip without change (mirror `LLMState`); doctest the round-trip incl. a
  tz-aware `benched_since` and an unknown extra key preserved.

Tests (`tests/test_models.py`): round-trip incl. empty/populated `quality`,
`None` vs set `benched_source`, unknown extra key preserved.

### Step 2 — registry reads/writes the learned profile

Files: `src/llmbroker/protocols/registry.py`, the three DB registries
(`sqlite`, `postgres`, `mongodb` — there is no `redis` registry, only a `redis`
state store), `src/llmbroker/standalone/registry.py`, `sqlite/schema.py`,
`postgres/schema.py`.

- Protocol: add `read_profiles(user_id) -> dict[str, LLMProfile]` and
  `write_profile(name, profile, user_id) -> None`. No new protocol file, no new
  backend type.
- SQL: add one JSON column `profile` (`JSONB`/`TEXT`) to `llmbroker_registry`.
  Keep `name`, `base_url`, `model`, `api_key_ref`, `user_id`, `metadata`. Use the
  **durable** version-gated `ensure_schema` path (additive
  `ALTER TABLE llmbroker_registry ADD COLUMN profile …` when the stored version
  marker is below the new `_SCHEMA_VERSION`; never `DROP`). `read_profiles`
  selects `(name, profile)` and parses `LLMProfile.from_dict`; `write_profile`
  `UPDATE`s **only** the `profile` column for `(name, COALESCE(user_id))`. The
  existing `sqlite`/`postgres` config `update()` is already safe by
  construction — it names an explicit `SET` column list (`base_url`, `model`,
  `api_key_ref`, `metadata`) that will simply not mention `profile`, so it
  cannot clobber it.
- Mongo stores the `to_dict()` document under a `profile` key in the same
  record; `read_profiles`/`write_profile` touch only that key. **Mongo needs an
  explicit fix, not just "don't touch it":** `mongodb/registry.py update()`
  today does a full-document `replace_one` with a `doc` dict it builds from
  scratch (`name`/`base_url`/`model`/`api_key_ref`/`metadata`/`user_id`) — once
  a `profile` key exists on stored documents, that `replace_one` call silently
  **deletes** it on every config update, since the replacement document simply
  never mentions it. Change `update()` to either fetch the existing document
  first and carry its `profile` forward into the replacement doc, or switch it
  to `update_one` with `$set` naming only the static fields (leaving `profile`
  untouched at the driver level) — the latter is preferable since it needs no
  extra read.
- File/TOML registry: config stays read-only from `llm.toml`/`.json`.
  `Registry.__init__(path, *, profile_path=None, persist_profile=True)`;
  `read_profiles` reads the sibling JSON (`<stem>.profile.json` by default, or
  `profile_path`), returns `{}` if missing or `persist_profile=False`;
  `write_profile` atomically upserts the model's entry in that sibling JSON, or
  is a no-op when `persist_profile=False`.

Tests (`tests/test_registry_*`): each backend round-trips an `LLMProfile`
(quality summary + a `manual` bench with tz-aware `since`); a future-proofing
extra key survives; config `update()` does **not** clobber a stored `profile`;
`SeedPolicy` static write leaves `profile` intact (assert directly). Migration
test (sqlite/postgres): seed an old version marker with no `profile` column, run
`ensure_schema`, assert the column now exists and pre-existing rows are **kept**.
File registry: default sibling path is derived from the config path; a
round-trip through `write_profile`/`read_profiles`; `persist_profile=False`
writes nothing and reads `{}`.

### Step 3 — optimizer: durable quality aggregate + auto-bench

File: `src/llmbroker/optimizer.py`.

- Feed `record_quality(call_id, score)` into a per-`(name, operation)` quality
  aggregate (rated_count / good_count against a `good` threshold), in addition to
  today's `score == 0 → mark_quality_fail`.
- Add config fields: `bench_floor: float`, `bench_margin: float`,
  `min_rated_samples: int` (reuse existing `min_sample_count` semantics where it
  fits).
- Add `evaluate_bench(name) -> BenchSource | None`: returns `AUTO` when every
  operation with `>= min_rated_samples` ratings is below `bench_floor` (and at
  least one such operation exists); returns `None` (un-bench) when some operation
  climbs above `bench_floor + bench_margin`; otherwise leaves the current verdict
  unchanged. Manual verdicts are never overridden here.
- Add `load_profiles(profiles: dict[str, LLMProfile])`: warm-start the quality
  aggregate from the persisted summaries on provision.
- Serialise the current aggregate back into an `LLMProfile` for persistence
  (`to_profile(name) -> LLMProfile`).

Tests (`tests/test_optimizer.py`): ratings below floor on all ops → `AUTO`;
below on one op but fine on another → not benched; recovery past
`floor + margin` → un-bench; no ratings → never benched; manual verdict is not
cleared by `evaluate_bench`; aggregate restored by `load_profiles` reproduces
`usable_rate`.

### Step 4 — pool overlay: benched = present-but-not-routable

Files: `src/llmbroker/broker/pool.py`, `src/llmbroker/broker/state.py`.

- Track a benched set in the pool. `add` enqueues a slot only when the config is
  **routable** = has a resolved key (onboarding plan) **and** not benched.
- Add `set_benched(name)` and `clear_benched(name)` (make routable again and
  enqueue if keyed). Benched is orthogonal to `cooldown`/`phase`. Note: by the
  time this plan lands, `_reenqueue_config` (onboarding plan) re-enqueues
  unconditionally on cooldown expiry — it no longer has a phase check to
  piggyback an exclusion on (`OFFLINE`/`PROBING` are gone). `set_benched` must
  add its own guard in `_reenqueue_config` (skip re-enqueue when the config is
  in the benched set), and `cool_down`'s cooldown-expiry callback must check it
  too — a benched model can still be mid-cooldown when it gets benched.
- `state()` / snapshots expose `benched` so `env` and admin views can show it.

Tests (`tests/test_pool.py`): a benched keyed config is in `configs` but never
acquired; `clear_benched` makes it acquirable; benching the last routable model
leaves the pool with zero routable slots.

### Step 5 — wire the profile through the broker

Files: `src/llmbroker/broker/broker.py`, `src/llmbroker/broker/catalog.py`,
`src/llmbroker/sync.py`.

- No new broker constructor argument: the profile is read/written through the
  **registry** the broker already holds.
- `ensure_pool` / provision: after `catalog.provision()` and after
  `seed_from_metrics`, call `registry.read_profiles(user_id)`, feed
  `optimizer.load_profiles(...)`, and apply each `benched` verdict to the pool
  (`pool.set_benched`).
- Auto-bench path: when `evaluate_bench` flips a verdict during the live event
  stream (`OptimizerTelemetry.record` / `record_quality`), persist the new
  `LLMProfile` via `registry.write_profile(...)` and reflect it in the pool.
  Debounce the aggregate-snapshot writes (verdict changes persist immediately;
  aggregate snapshots persist on a debounce and on `aclose`).
- Manual API: `AsyncBroker.disable_llm(name, *, reason=None)` /
  `enable_llm(name)` — set/clear a `MANUAL` bench via `registry.write_profile`
  and the pool; `enable_llm` also clears any `AUTO` bench and suppresses re-bench
  for the rating window. Proxy both on the sync `Broker`.

Tests (`tests/test_broker_bench.py`, `tests/test_sync.py`): a persisted `benched`
profile is applied at provision (model not routable); a live auto-bench persists
through the registry and withdraws the slot; `disable_llm`/`enable_llm`
round-trip through the registry and the pool; sync proxies work.

### Step 6 — `SeedPolicy.SYNC` default

Files: `src/llmbroker/models.py`, `src/llmbroker/broker/catalog.py`,
`src/llmbroker/broker/broker.py`.

- Add `SeedPolicy.SYNC = "sync"`. In `apply_seed`, `SYNC` upserts (add new,
  update existing static config) and **does not remove** configs missing from the
  source.
- Change the default `seed_policy` on `AsyncBroker` (and any preset wiring) from
  `IF_EMPTY` to `SYNC`.
- Confirm `apply_seed` writes only static fields (`base_url`, `model`,
  `api_key_ref`, `metadata`) — it must never write `profile` — so learned data
  is safe across re-seed.

Tests (`tests/test_catalog_seed.py`): `SYNC` adds a preset-new model, updates a
changed `rate_limit` on an existing one, and leaves a user-only model (dropped
from the preset) in place; a benched model's `profile` survives a `SYNC` re-seed
and is **not** re-routed (verdict intact); the default policy is `SYNC`.

### Step 7 — docs

Files: `README`/docs, `specs/reference/architecture.md`.

- Document the two-halves catalog (curated static vs learned dynamic) as one
  registry with two field groups, profile persistence (DB column always on; file
  registry sibling JSON, opt-out via `persist_profile=False`), `benched` (auto vs
  manual), and that `SYNC` is the default so catalog updates reach users without
  losing learned data.

## Non-goals

- **A separate profile backend.** The learned profile is a field of the registry
  row (DB column / file-registry sibling JSON), not a fifth store type.
- **Moving `cooldown`/`phase` into the profile.** Those are ephemeral
  sync-state; only durable learned knowledge lives in the profile.
- **A telemetry/crowdsourcing pipeline.** The quality aggregate is this
  deployment's own ratings materialised for cheap reads and restart survival —
  not shared or uploaded (consistent with the onboarding plan's non-goals).
- **Quality-ranked routing beyond the existing soft floor.** Bench is a binary
  exclusion for globally-useless models; per-request quality routing stays the
  existing per-operation soft floor.
- **Removing dropped models on re-seed by default.** `SYNC` keeps them idle;
  `MIRROR` remains for callers who explicitly want pruning.

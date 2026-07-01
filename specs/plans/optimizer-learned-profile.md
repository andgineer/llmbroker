# Learned LLM profile — the dynamic, optimizer-owned half of the catalog

## Plan sequence — step 3 of 4

> **Prerequisites:** `db-schema-resilience.md` (step 1) — reuse its typed
> dataclass ⇄ JSON-document boundary and durable version-gated `ensure_schema`
> path for the new profile store — **and** `preset-onboarding-effort.md`
> (step 2) — this plan extends its keyless-not-routable pool change and its
> zero-routable alarm (`routable ⟺ keyed **and** not benched`). Do the
> onboarding pool change first. **Blocks:** nothing.

The four plans form one dependency chain; execute in this order:

1. **`db-schema-resilience.md`** — storage-shape foundation: columns-vs-JSON;
   defines `RateLimit`, `LLMConfig.rate_limit`, the `LLMState` ⇄ dict boundary,
   and the version-gated `ensure_schema` toolkit.
2. **`preset-onboarding-effort.md`** — curated catalog knowledge, effort/value
   onboarding, warm-start seeding, the `EXHAUSTED` phase, and the
   keyless-not-routable pool change.
3. **`optimizer-learned-profile.md`** *(this plan)* — the durable learned half
   (profile store, bench verdict) and `SeedPolicy.SYNC`; extends the routable
   predicate from (2).
4. **`catalog-refresh.md`** — the manual re-curation runbook; consumes the
   taxonomies fixed in (2) and may run in parallel with this plan.

## Problem statement

llmbroker's concept is shifting: the LLM list is **curated by the maintainers**
and shipped as a preset; the end user mostly just supplies keys and does not
care which models are inside — they want it to work and not be nagged for new
keys. Under that concept the persisted data splits cleanly by *owner* and
*durability*, and today one quadrant has no home:

| Layer | Written by | Durability |
|---|---|---|
| **Curated catalog** (`llmbroker_registry`) | preset roll / admin only | durable |
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

## Concept: the catalog has two halves

Logically the LLM catalog is one thing, but it has a **static half** and a
**dynamic half**, and they must be stored separately so a catalog refresh can
overwrite one without touching the other:

- **Static half — the curated catalog** (`llmbroker_registry`): `name`,
  `base_url`, `model`, `api_key_ref`, and curated per-model config
  (`rate_limit`, …). Owned by the preset / admin. The optimizer never writes
  here. Freely overwritten by a preset roll.
- **Dynamic half — the learned profile** (new store): what *this deployment* has
  learned about each model. Owned by the optimizer (with manual admin
  overrides). Never overwritten by a preset roll.

The learned profile carries two things, both genuinely per-model and both more
than a lone flag:

- the **quality aggregate** — a compact, durable summary of the model's rated
  usefulness (the persisted form of `usable_rate`), so warm-start survives
  restart and does not require scanning the call log;
- the **bench verdict** — `benched` + `source (auto|manual)` + `since`, the
  standing decision to exclude a model from routing while keeping its stats.

A "benched" model is deliberately distinct from the ephemeral lifecycle
`phase` (`AVAILABLE/COOLING/OFFLINE/PROBING/EXHAUSTED`): those are transient and
live in the disposable sync-state; `benched` is a durable verdict. `EXHAUSTED`
(from [`preset-onboarding-effort.md`](preset-onboarding-effort.md)) stays in the
ephemeral state and does **not** move here.

## The profile store

A new optional backend, parallel to the state store, keyed by
`(llm_name, user_id)`:

- **Protocol** `ProfileStoreProtocol` (`src/llmbroker/protocols/profile.py`):
  `read(user_id) -> dict[str, LLMProfile]` and
  `write(name, profile, user_id) -> None`. Queryable/mutable shape mirrors
  `StateStoreProtocol` so the four DB backends and the standalone file backend
  can implement it uniformly.
- **`LLMProfile`** (`src/llmbroker/models.py`, one typed dataclass ⇄ dict
  boundary, same pattern as `LLMState`): a per-operation quality summary plus the
  bench verdict fields. Serialises to a single JSON document — reuse the
  columns-vs-JSON decision from
  [`db-schema-resilience.md`](db-schema-resilience.md): `llm_name`, `user_id`
  stay columns; everything else is one JSON body.
- **Durability is real and separate.** Unlike the state store (dropped on schema
  bump), the profile store must migrate its rows, not discard them — its whole
  reason to exist is surviving churn. Use the durable-`ALTER`/version-gated path
  from `db-schema-resilience.md`, never the `DROP TABLE` path.
- **Standalone backend** `Profile(path)`: a JSON file, so the no-DB deployment
  still gets durable learned data (the deployment that "is literally a log" gains
  a proper place for these properties instead of hanging them off the log).
- **Default `None`** (in-memory, process-lifetime), exactly like `state_store`:
  persistence is opt-in; when unconfigured, learned data and manual bench last
  only for the process, which is an accepted trade-off for zero-config use.

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
- Because the learned profile lives in a **separate store**, `apply_seed`
  (registry + secrets only) structurally cannot touch it — the "never clobber
  learned data" guarantee is architectural, not a merge rule to get right.
- `MIRROR` (removes dropped models) and `ADD` / `IF_EMPTY` remain available for
  callers who want them; only the default changes.

Note `SYNC`'s `update` overwrites the **static** registry config (e.g. a revised
`rate_limit`) from the preset — which is correct, that is the curated half doing
its job — while the profile store's quality aggregate and bench verdict are
untouched.

## Relationship to the other plans

- [`db-schema-resilience.md`](db-schema-resilience.md): reuse its typed
  dataclass ⇄ JSON-document boundary and its version-gated `ensure_schema`
  pattern for the new profile store. The profile store uses the **durable**
  (`ALTER`, keep rows) path, contrasting the state store's disposable
  (`DROP`) path. No new decision — same toolkit.
- [`preset-onboarding-effort.md`](preset-onboarding-effort.md): this plan
  composes with its keyless-not-routable pool change (routable ⟺ keyed **and**
  not benched) and its zero-routable alarm. Do the onboarding pool change first;
  this plan extends the same predicate. `EXHAUSTED` stays in ephemeral state.

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

### Step 2 — `ProfileStoreProtocol` + backends

Files: `src/llmbroker/protocols/profile.py` (new), the four DB profile stores
(`sqlite`, `postgres`, `mongodb`, `redis`), `src/llmbroker/standalone/profile.py`
(new JSON-file `Profile`), `sqlite/schema.py`, `postgres/schema.py`.

- Protocol: `read(user_id) -> dict[str, LLMProfile]`,
  `write(name, profile, user_id) -> None`; `AsyncResourceProtocol.aclose()` where
  a backend holds a resource.
- SQL: `llmbroker_profile` with `llm_name`, `user_id` columns + one JSON
  `profile` column (`JSONB`/`TEXT`), unique index on
  `(llm_name, COALESCE(user_id))`. Use the **durable** version-gated
  `ensure_schema` path (create if missing; `ALTER`/additive on upgrade; never
  `DROP` — rows are the valuable data).
- Redis/mongo store the `to_dict()` document per name.
- `Profile(path)` standalone: JSON file, atomic write, read-through on load.

Tests (`tests/test_profile_store_*`): each backend round-trips an `LLMProfile`
(quality summary + a `manual` bench with tz-aware `since`); a future-proofing
extra key survives; a migration test seeds an old version marker and asserts rows
are **kept** (not dropped) after `ensure_schema`.

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
- Add `set_benched(name)` (withdraw the slot; benched models are not re-enqueued
  by `_reenqueue_config`) and `clear_benched(name)` (make routable again and
  enqueue if keyed). Benched is orthogonal to `cooldown`/`phase`.
- `state()` / snapshots expose `benched` so `env` and admin views can show it.

Tests (`tests/test_pool.py`): a benched keyed config is in `configs` but never
acquired; `clear_benched` makes it acquirable; benching the last routable model
leaves the pool with zero routable slots.

### Step 5 — wire the profile store into the broker

Files: `src/llmbroker/broker/broker.py`, `src/llmbroker/broker/catalog.py`,
`src/llmbroker/sync.py`.

- `AsyncBroker.__init__`: accept `profile_store: ProfileStoreProtocol | None =
  None`; default `None` (in-memory, like `state_store`).
- `ensure_pool` / provision: after `catalog.provision()` and after
  `seed_from_metrics`, read the profile store, call
  `optimizer.load_profiles(...)`, and apply each `benched` verdict to the pool
  (`pool.set_benched`).
- Auto-bench path: when `evaluate_bench` flips a verdict during the live event
  stream (`OptimizerTelemetry.record` / `record_quality`), persist the new
  `LLMProfile` to the profile store and reflect it in the pool. Debounce the
  aggregate-snapshot writes (verdict changes persist immediately; aggregate
  snapshots persist on a debounce and on `aclose`).
- Manual API: `AsyncBroker.disable_llm(name, *, reason=None)` /
  `enable_llm(name)` — set/clear a `MANUAL` bench in the profile store and the
  pool; `enable_llm` also clears any `AUTO` bench and suppresses re-bench for the
  rating window. Proxy both on the sync `Broker`.

Tests (`tests/test_broker_bench.py`, `tests/test_sync.py`): a persisted `benched`
profile is applied at provision (model not routable); a live auto-bench persists
and withdraws the slot; `disable_llm`/`enable_llm` round-trip through the store
and the pool; sync proxies work.

### Step 6 — `SeedPolicy.SYNC` default

Files: `src/llmbroker/models.py`, `src/llmbroker/broker/catalog.py`,
`src/llmbroker/broker/broker.py`.

- Add `SeedPolicy.SYNC = "sync"`. In `apply_seed`, `SYNC` upserts (add new,
  update existing static config) and **does not remove** configs missing from the
  source.
- Change the default `seed_policy` on `AsyncBroker` (and any preset wiring) from
  `IF_EMPTY` to `SYNC`.
- Confirm `apply_seed` touches only registry + secrets — the profile store is
  never passed to it, so learned data is structurally safe across re-seed.

Tests (`tests/test_catalog_seed.py`): `SYNC` adds a preset-new model, updates a
changed `rate_limit` on an existing one, and leaves a user-only model (dropped
from the preset) in place; a benched model's profile survives a `SYNC` re-seed
and is **not** re-routed (verdict intact); the default policy is `SYNC`.

### Step 7 — docs

Files: `README`/docs, `specs/reference/architecture.md`.

- Document the two-halves catalog (curated static vs learned dynamic), the
  profile store as opt-in durability, `benched` (auto vs manual), and that
  `SYNC` is the default so catalog updates reach users without losing learned
  data.

## Non-goals

- **Moving `EXHAUSTED` or any cooldown/phase into the profile.** Those are
  ephemeral sync-state; only durable learned knowledge lives in the profile.
- **A telemetry/crowdsourcing pipeline.** The quality aggregate is this
  deployment's own ratings materialised for cheap reads and restart survival —
  not shared or uploaded (consistent with the onboarding plan's non-goals).
- **Quality-ranked routing beyond the existing soft floor.** Bench is a binary
  exclusion for globally-useless models; per-request quality routing stays the
  existing per-operation soft floor.
- **Removing dropped models on re-seed by default.** `SYNC` keeps them idle;
  `MIRROR` remains for callers who explicitly want pruning.

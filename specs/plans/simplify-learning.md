# Simplify learning, alerts, and seeding (Plan 2 of 3)

Prerequisite: `specs/plans/simplify-core.md` (Plan 1) is fully landed and green.
Follow-up: `specs/plans/simplify-storage.md` (Plan 3).
Mission context and open decisions: `specs/mission-cost.md`.

This plan **changes implementation mechanics, not the learning capability**:

- Learning stays **per (model, operation)**; selection becomes one demoted-last
  sort — the tier concept is deleted (2.2; `deprecated` dies with
  sync-deletes, 2.5). What changes is the machinery: sliding windows of raw
  scores with a Wilson upper bound replace decayed aggregates and the bandit
  apparatus (exploration, floors, latency ranking, auto-retirement).
- Learned state is **derived from the call journal** instead of being a second
  storage subsystem. The journal persists every rating and is
  **append-only, so concurrent instances cannot clobber each other** — this
  replaces both the summaries/fold machinery and any snapshot merging. No
  last-writer-wins anywhere. No fold-in-place aggregates even where the backend
  could increment atomically: the default jsonl backend cannot, so row append +
  debounced re-aggregation is the single mechanism for all backends.
- **Shared cooldowns are derived from the journal too** — the state store
  ceases to exist. A failed call row (429/503) is written anyway; it gains a
  `cooldown_until` field computed by the instance that caught the failure.
  Peers' cooldowns = max(`cooldown_until`) over recent failed rows, obtained
  from the **same** debounced journal read as the score windows, plus an
  immediate refresh on an own failure. No separate TTL/2s cache exists.
  Coordination is advisory: failover is the correctness mechanism; the cost of
  staleness is one wasted roundtrip that fails over transparently.
- **Scopes narrow to keys, and the scope is an opaque string** (decisions in
  `mission-cost.md`): the registry and learning are always global — score
  windows, verdicts, and metrics aggregate the whole journal regardless of
  scope. The typed `user_id` on the broker facade is replaced by
  `scope: str | None`; it prefixes secret refs (own key, falling back to the
  shared one) and attributes journal rows — storage never learns about users.
  Quota failures follow the key: a 429/401/403 row carries a short hash of the
  key used; peer cooldowns and dead-key drops apply only where the hash matches
  the instance's current key; 5xx rows apply unconditionally.
- The knowledge store is llmbroker-internal, **not logging**: plain-text
  `Telemetry` is deleted; the default becomes a `state/` directory next to the
  TOML config (`state/calls/YYYY-MM-DD.jsonl` journal + `state/disabled.yml`);
  the journal self-purges on a retention horizon, the disabled map is never
  purged (2.7).
- The preset file is the only source of model definitions; `sync` mirrors it
  into the registry — a total mirror, nothing to preserve. Model CRUD,
  `origin`, and `copy_registry` do not exist; **nothing but `sync` writes the
  registry**. The admin's runtime writes — `set_disabled(name, flag)` and key
  edits — never touch it. `deprecated` is deleted — sync deletes absent
  entries instead of deprecating them.
- The telemetry subsystem is renamed and widened to **knowledge**: everything
  llmbroker knows about models beyond config — the append-only **journal**
  plus a tiny mutable **disabled map** of admin verdicts (`name → bool`).
  `set_disabled` writes the map, so a verdict survives any `sync` by
  construction and works identically for file and DB sources. `LLMProfile`
  (the llmbroker-written registry column) is deleted; the optimizer never
  writes the map, llmbroker itself only seeds missing model names with
  `disabled: false` and never changes values.
- The `alerts()` API is deleted entirely: the three important events become log
  lines; the UI pulls current state via `snapshot()` raw fields — no status
  enum exists.
- `seed=` disappears entirely: an explicit `sync(preset)` call replaces
  implicit seed-on-start (which flip-flops the registry in a cluster whose nodes
  hold diverged TOML copies).
- Key onboarding metadata becomes a TOML passthrough.

Durability rule (user-approved): learning persists across process runs by
default (the file knowledge store); only the explicit in-memory opt-out leaves
windows and verdicts in process memory.

## Rules for the implementer (read first)

- Lint/format/type-check only via `invoke pre` (never call ruff directly). Tests via
  `python -m pytest`. Both must be green after **every numbered step**.
- `pytest.ini` runs `--doctest-modules`: doctests in `src/` execute as tests.
- No in-function imports; no `from __future__ import annotations`; Python 3.11+.
- Never edit `src/llmbroker/__about__.py`; never bump the version.
- Never use `pytest.skip`/`importorskip`/`skipIf`.
- Never delete a test that reproduces a confirmed bug; port it to the new surface.
- Comments: 1–2 lines, non-obvious WHY only.

---

## 2.1 Replace the statistics engine with per-(model, operation) score windows

`optimizer.py` (407 lines) — same verdict surface, simpler internals.

**Keep (verbatim semantics):**

- `max_delay`, `backoff_factor`, `rl_fail_count` / `on_rate_limited` /
  `on_success` — cooldown/backoff bookkeeping the router reads.
- The manually-disabled set (`set_disabled`/`clear_disabled` after Plan 1's
  rename of the bench vocabulary).
- Dead-key handling: HTTP 401/403 → drop from pool + `logger.error` naming the
  `api_key_ref` (currently in `OptimizerTelemetry._drive_fsm`,
  `optimizer.py:348-356`; the alert emission dies in 2.4). The failed row
  carries `key_hash` (2.3), so peers holding the same key drop the model too;
  the debounced registry re-read re-adds it once the admin replaces the key —
  the re-add resolves the new key, whose hash no longer matches the dead rows.
- The verdict API shape used by the pool: per-operation demotions —
  reimplemented on windows below.

**Delete:**

- The decayed-summary dicts `_ranking`/`_latency`/`_quality` and their math:
  `usable_rate`, `mean_latency_ms`, `_RANK_N`/`_D_RANK`,
  `transport_decay`/`quality_decay`, `to_profile`, `load_summaries`. The Wilson
  formula itself survives, re-based onto windows: keep `_z_score` and a
  window-level `wilson_upper(scores)` (~30 lines total).
- Knobs of the deleted math: `quality_effective_n` (superseded by
  `quality_min_count`), `quality_margin`, `demotion_realert_interval`,
  `min_sample_count`, `usable_rate_floor`, `removal_rate_floor`,
  `exploration_fraction`, `background_operations`. `quality_confidence` (the
  Wilson z) stays.
- `OptimizerPolicy`, `FirstAvailablePolicy`, the `SelectionPolicy` protocol and
  the `policy=` parameter of `pool.acquire` (selection is one sort key, 2.2).
- Automatic retirement (`should_retire`): chronic transport failure is already
  handled by exponential cooldowns up to `max_delay`; the dead-key drop is the
  only automatic removal left.
- `QualitySummary` in `models.py` once the last importer is gone (state-store
  uses die in Plan 3).

**New learning core (~60 lines):**

```python
@dataclass
class Optimizer:
    max_delay: float = 3600.0
    backoff_factor: float = 2.0
    quality_floor: float = 0.3
    quality_confidence: float = 0.95  # z for the Wilson upper bound
    quality_window: int = 30       # ratings kept per (model, operation)
    quality_min_count: int = 10    # verdicts need at least this many

    _scores: dict[tuple[str, str | None], deque[float]]  # deque(maxlen=quality_window)

    def record_quality(self, name: str, operation: str | None, score: float) -> None: ...
    def is_demoted(self, name: str, operation: str | None) -> bool:
        """len >= quality_min_count and wilson_upper(window) < quality_floor."""
    def demoted_operations(self, name: str) -> frozenset[str | None]: ...
    def load_scores(self, scores: dict[tuple[str, str | None], list[float]]) -> None: ...
        # replace windows wholesale — used by the journal rebuild (2.3)
```

Demotion flip detection (for the log line): compare the per-op verdicts
before/after each `record_quality`/`load_scores`; on False→True emit
one `logger.warning`; True→False logs
`info`. No standing re-alerts. There is no global verdict (`is_globally_demoted`
is not reimplemented): demotion is always per (model, operation).

## 2.2 Selection: demoted-last sort — tiers deleted as a concept

Selection is one sort key (decision in `specs/mission-cost.md`):

```python
min(candidates, key=lambda s: (optimizer.is_demoted(s.config.name, operation), s.order))
```

where `_Slot.order` is the registry/preset index. Models demoted for this
operation go last (still reachable as last resort — a demoted-only pool
serves); everything else follows curated priority: best model takes traffic
until it cools, 429 never reaches the caller (failover retries the next model
within the same request), cost is one extra roundtrip on the request that hits
quota exhaustion.

This deletes the tier concept entirely: `_partition_by_tier`, `pool.tier_of`,
`update_demotions`, `_demoted_operations`, `_globally_demoted`,
`_refresh_demotions` pushes, plus `LLMConfig.deprecated`, `_Slot.deprecated`
(Plan 1 carries it only as a mirror of current behavior) and `is_deprecated`.
The global verdict is deleted entirely — demotion is always per (model,
operation); `snapshot()` exposes the per-model list (2.4). Round-robin
(`pick_seq` from Plan 1) is dropped.

## 2.3 Learned state derived from the call journal (no second storage subsystem)

**Facts this builds on:** every call row already carries `llm_name`, `operation`,
`status`, `latency_ms`, `called_at`. The journal becomes **strictly
append-only in every backend**: `record_quality` appends its own quality
record everywhere, DB backends included (jsonl already does; the DB row-update
via `set_field` dies, Plan 3 drops the driver op). A quality record is
**self-contained** — it carries `(llm_name, operation, score)` itself, so no
join with call rows exists at rebuild (`call_id` may ride along as an opaque
host-UI reference; the rebuild never reads it). This also closes a rating-loss
hole: a rating whose call row has already left the rebuild tail (or an old day
file) still counts. Concurrent instances add records, never
overwrite — cluster consistency needs no folds and no merging.

**Mechanism:**

- **Own ratings apply instantly**: `record_quality` appends to the in-memory
  window (2.1) in the same call that writes the journal row.
- **Everything derived comes from one cached tail read**: re-derive on a
  debounce (`_REBUILD_TTL = 60.0` seconds, checked on activity — no background
  task, an idle broker does zero reads). Rebuild = fetch the most recent
  records (reuse `calls(limit=quality_rebuild_limit)`, default 300 — one
  query, portable; the read is **unscoped** — the tail spans all scopes,
  attribution survives only as a filter of the `calls()` query API)
  and split in Python: rated records feed the score windows
  (bucket by `(llm_name, operation)`, keep the newest `quality_window` per
  bucket, `optimizer.load_scores`); failed records feed shared cooldowns
  (below); all records feed `LLMMetrics`. At provision, run one rebuild as the
  warm start.
  The file knowledge store supports the same rebuild by reading its day files
  newest-first (up to `quality_rebuild_limit` records) — the default setup
  learns across runs. Only the in-memory opt-out degrades to session-scoped
  learning.
- **The same read yields shared cooldowns**: `Call` gains
  `cooldown_until: datetime | None`, set on 429/503 rows from
  `Retry-After`/backoff by the failing instance, and
  `key_hash: str | None` — a short digest (first 12 hex chars of SHA-256) of
  the resolved key, set on failed rows. The rebuild filters recent
  failed rows and takes max(`cooldown_until`) per llm — 5xx rows
  unconditionally (provider-side, shared by everyone), 429 rows only when
  `key_hash` matches the instance's current key for that llm (quota belongs
  to the key: a shared key shares its cooldown, a personal key cools only its
  owner); 401/403 rows with a matching hash re-apply the dead-key drop. The
  result feeds `pool.apply_shared_cooling`; an own failure triggers an out-of-band refresh
  (`maybe_rebuild(force=True)`), because coordination only matters around
  failures. The shared fail-streak is the count of recent failed rows. This
  replaces the state store entirely — its 2s cache, `reconcile()`, protocol,
  and table die here; Plan 3 deletes the backend implementations and Redis.
  A fresh (stateless) process is informed by its warm-start read.
- **The same debounce also refreshes snapshot metrics and the registry**:
  `LLMMetrics` (count / last status / last call time per model) is computed
  from the cached tail — the `metrics_rows` driver op dies (Plan 3) and
  `snapshot()` performs no DB reads; metric semantics become "over the last
  `quality_rebuild_limit` records" (user-approved). The rebuild additionally
  re-reads the registry and the disabled map (4–5 rows plus a tiny
  document) so `sync` results, key changes, and admin verdicts from other
  processes/nodes take effect on running brokers without restarts.
- **Forgetting** = journal retention (2.7): backends self-purge rows older
  than the retention horizon, so old ratings age out of rebuilds. The public
  `purge_calls` API is deleted.

**Optional per-user keys via the scope string.** `AsyncBroker`/`Broker`
replace `user_id: int | str | None` with `scope: str | None` — an opaque
non-empty string (e.g. `"user/42"`; the facade rejects `""`, which replaces
`check_user_id`). Key resolution (catalog, at provisioning) tries
`resolve(f"{scope}/{ref}")` and falls back to `resolve(ref)` on `KeyError` —
the fallback policy lives in the broker, written once; secrets backends stay
exact-lookup key-value stores. In this plan the existing backends keep their
`user_id` parameter — the broker simply always passes `None` (the scope rides
inside the ref string); Plan 3 deletes the parameter from the protocols and
backends. Env secrets need no special case: the prefixed name is simply not
set, so resolution falls through to the shared ref — today's behavior.
Journal rows carry `scope` as a plain string field (attribution only, the
`calls(scope=…)` filter; llmbroker never interprets it). `key_hash` is
computed from the resolved key **value**, so identical key values (env
secrets; duplicated rows) coalesce into one quota scope by construction — no
own/shared labeling is needed anywhere.

**What this deletes:** `_ProfileSync` and `_ProfileSyncTelemetry` entirely
(`broker/broker.py:60-311`), `seed_summary`/`apply_summary_delta`/`read_summaries`
call sites, `_REFRESH_TTL`/`_SNAPSHOT_DEBOUNCE`, `write_snapshot`/
`write_all_snapshots`/`reset_quality`'s state-store loop, `peek_call`,
`StateStoreProtocol` and the `state_store=` parameter (shared cooling now comes
from the journal; Plan 3 deletes the backend state-store modules and Redis).

**`LLMProfile` (llmbroker-written registry state) is deleted; the manual block
becomes an admin verdict in the knowledge disabled map.** Auto-demotion is soft —
a demoted model still serves as last resort — so the hard cut for a model the
admin judged useless is `set_disabled(name, True)` — one method, not a
disable/enable pair — and it writes the knowledge **disabled map**: a tiny
mutable document owned exclusively by the admin path.
Not a registry field: the registry is a pure preset mirror, so the verdict
survives any `sync` by construction and needs no overlay next to the file
source (the TOML is never written; the map lives in `state/`). Not a
journal record: retention would silently erase the verdict. Learning verdicts
stay derived-on-read — the optimizer never writes the map, and llmbroker
only **seeds missing model names** with `disabled: false` (at `sync` for DB
sources, at provisioning for the file source) without ever changing existing
values, so the admin edits values only. Shape: flat mapping
`model name → disabled: bool` (DB: `llmbroker_disabled` keyed by name, Plan 3;
file source: `state/disabled.yml` — YAML for comfortable hand-editing, PyYAML
becomes a core dependency, user-approved). Reading the verdict is first-class:
the `get(name)` handle exposes a `disabled` property served from the cached
map, and `snapshot()` carries the same field (2.4) — no extra reads.
`disabled_since`/`disabled_reason` are dropped (the host's admin UI can log
its own reasons); say so in the spec. The debounced refresh (above) re-reads
the map together with the registry, so verdicts from other
processes/nodes propagate without restarts; an own verb applies instantly.
`snapshot()` carries the same field (2.4). Delete `LLMProfile`
and the registry-side `write_profile`/`read_profiles`.
The "bench" vocabulary dies with the rename (Plan 1 renames the pool side:
`set_disabled`/`clear_disabled`/`is_disabled`).
There is no quality reset (user decision): `set_disabled(name, False)` only
clears the verdict; a re-enabled model rehabilitates through new ratings — the window
keeps the newest `quality_window` scores, so fresh ratings displace the old
ones. `Optimizer.reset_quality` is deleted along with the former reset marker.

**The one remaining knowledge-store wrapper** replaces the current two-wrapper
chain in `broker/broker.py`:

```python
class _LearningHook:
    """Knowledge hook: cooldown bookkeeping, dead-key drops, quality windows,
    debounced journal rebuild."""
    async def record(self, call): ...          # keep try/finally semantics (optimizer.py:276-283)
    async def record_quality(self, llm, operation, score, call_id=None): ...
    async def maybe_rebuild(self, *, force=False): ...
```

The public `record_quality` changes accordingly: the caller passes
`(llm, operation, score)` taken from the `ask()` result; `call_id` is an
optional passthrough for the host's journal UI — llmbroker never resolves it.

## 2.4 The `alerts()` API is deleted; events are log lines; raw facts in `snapshot()`

Delete the alerts API entirely (user decision, see `mission-cost.md`):
`alerts()`, `add_alert`, the drain-on-read list, and the `Alert` model. The
three important situations remain as logging only, un-debounced except (c):

- (a) dead key (401/403 drop) — `logger.error` naming the `api_key_ref`;
- (b) demotion flip — per-operation (2.1), `logger.warning`;
- (c) all keyed models cooling simultaneously — `logger.warning`; keep the
  single `_last_underprov_alert` timestamp inline (`broker.py:593-616`) as a
  60s log debounce.

The former "seed refusing a `model` identity change" alert dies with implicit
seeding: `sync` is an explicit call (2.5), so the refusal surfaces as a
synchronous error/report to the caller.

Also delete: degraded-tier alerts (`pool._maybe_alert_degraded_tier`, the
`on_degraded_tier` callback and wiring), floor-drop alerts (die with
`OptimizerPolicy`), standing re-alerts, and the four debounce maps
(`_demotion_alert_times`, `_global_alert_times`, `_last_degraded_alert`,
`_last_floor_alert`).

For the UI (pull model): there is **no status enum** — `LLMSnapshot` carries
the raw facts and the host derives whatever presentation it wants:
`disabled: bool` (the admin verdict, same value the `get(name)` handle
exposes), `has_key: bool`, `cooldown_until: datetime | None`,
`demoted_operations: tuple[str, ...]`, and metrics. No status-precedence rule
and no global verdict exist anywhere — and the UI gets richer for free
("demoted for summarize" instead of one lumped state). `LLMMetrics` is served from the
cached journal tail (2.3): `snapshot()` performs zero DB reads. The host polls
`snapshot()` — a host that wants event history diffs snapshots or hooks the
`llmbroker` logger.

## 2.5 Explicit `sync` mirrors the preset; model CRUD is deleted

Implicit seed-on-start is unsound in a cluster: each node would reconcile the
registry against its local TOML copy, and diverged copies make the registry
flip-flop. Seeding becomes an explicit call the operator makes when a fresh
preset is downloaded or the application DB is initialized.

The preset file is the **only source of model definitions** (decision in
`mission-cost.md`); the registry is its pure mirror. `sync(preset)`
semantics: add new entries, update existing ones, **delete absent ones**
(replaces deprecation — nothing is lost: keys live in the secrets store,
learned state is derived from the journal, journal rows stay until retention,
verdicts stay in the knowledge disabled map; a model returning to the preset is
simply re-added and its ratings and verdict resurface); bootstrap missing
secrets from env; **seed missing disabled-map entries** (every model
name present with `disabled: false`, existing verdict values never touched —
so the admin only flips values by hand or via the verbs). Refusing a
`model`-identity change stays, as a synchronous error to the caller — it
protects the binding of learned stats to the model name. A host with its own
models keeps its own preset file.

- Delete the `SeedPolicy` enum (`models.py`) and the `seed=`/`seed_policy=`
  parameters from `AsyncBroker`/`Broker` and stack paths; the
  `IF_EMPTY`/`ADD`/`MIRROR` branches in `catalog.apply_seed`
  (`catalog.py:137-155`) die — the mirror is the only path.
- Delete model CRUD everywhere: the `add`/`remove`/`update` verbs on the
  broker, catalog, CLI, and registry protocol; the `origin` field of
  `LLMConfig` (and its storage representation); and `copy_registry` — syncing
  from any registry-shaped source already covers copying. The admin's runtime
  writes — `set_disabled` (writing the knowledge disabled map) and
  secrets edits — never touch the registry.
- Add `AsyncBroker.sync(preset)` / `Broker.sync(preset)` (preset = path or
  registry) and a CLI touchpoint (`python -m llmbroker sync <preset> ...`)
  for DB-init workflows.
- Provisioning against an **empty registry fails fast** with an error telling
  the user to call `sync(preset)` — no silent empty pool.
- **The registry becomes globally scoped** (decision in `mission-cost.md`):
  one model list, one `sync` for all users. The broker, catalog, and `sync`
  stop scoping registry operations by user (pass `None` for now — Plan 3
  removes the parameter from the registry protocol and the `user_id` column
  from the table). `AsyncBroker`/`Broker` expose `scope=` instead of
  `user_id=` (2.3): it prefixes secret refs and attributes journal rows only.
- Update `architecture.md`: replace the `SeedPolicy` table and policy text
  with the mirror `sync` (current state only, no history).

## 2.6 Key onboarding metadata: plain passthrough

Keep the `key_info()` optional capability, drop the taxonomy machinery: `KeyInfo`
becomes `help: str` plus a free-form `extra: dict[str, str]` passthrough of the
TOML section; delete `EffortLevel`/`ValueLevel` and the ordering logic;
`python -m llmbroker env <config>` prints keys in **file order** with their help
lines. Update the "Key acquisition help" section of `architecture.md`.

## 2.7 Knowledge store: `state/` default, record format, retention; plain-text `Telemetry` deleted

The knowledge store (journal + disabled map) is llmbroker's internal subsystem, not
application logging — an app that wants logs uses `logging` itself. The
telemetry vocabulary is renamed throughout (protocols, classes, the
`telemetry=` parameter becomes `knowledge=`). Consequences:

- **Delete `standalone.Telemetry`** (the python-logging emitter). Remaining
  knowledge backends: the file store in `state/` (default; supersedes
  `JsonlTelemetry`), DB-backed (same DB as the source), and the explicit
  in-memory opt-out (supersedes `NoTelemetry`; memory-only learning and
  verdicts).
- **Default wiring** (`broker.py:344-348`): with a file/TOML registry (a path
  or a standalone `Registry`) and no `knowledge=`, construct the file store
  in a `state/` directory sibling to the TOML config; with a DB source, the
  same DB (Plan 3 replaces `stack=` with the single source parameter — this
  default-resolution rule carries over). Any other registry (a bare DB
  registry, a custom object) with no explicit `knowledge=` falls back to
  `./state` under CWD (user-approved: not an error).
- **One journal file per day** (user-approved): `state/calls/YYYY-MM-DD.jsonl`,
  chosen by the record's UTC date. Not aggregation — pure storage layout:
  rebuild needs raw scores (windows of last N ratings), and quality records
  rate calls from earlier days, so day files stay append-only and are never
  reopened/rewritten.
- **Disabled file**: `state/disabled.yml` — the admin-verdict document (2.3), a
  flat `model name → disabled` mapping. Pre-seeded with all model names at
  provisioning (missing names appended, values never changed by llmbroker),
  hand-edited or written by `set_disabled`; picked up by the
  debounced refresh; excluded from retention. YAML via PyYAML (core
  dependency, user-approved — hand-editing is the point). Example:

  ```yaml
  # llmbroker: admin verdicts; values are yours, names are seeded automatically
  gemini-2.0-flash: false
  groq-llama-3.3-70b: false
  mistral-small: true        # hallucinates on summarize
  ```
- **Record format** (`standalone/telemetry.py`): one JSON object per line,
  `kind: call | quality`; a quality record is self-contained (`llm`,
  `operation`, `score`, optional `call_id` passthrough — never joined); a call
  record carries `scope` when the broker has one (2.3); drop
  `None` fields at serialization, add `ts` (aware
  UTC ISO-8601) to every record — today records carry no timestamp at all
  (`called_at` exists only in DB backends). Rebuild (2.3) reads day files
  newest-first until it has `quality_rebuild_limit` records.
- **Retention instead of `purge_calls`**: every journal backend self-purges
  records older than `retention` (constructor parameter, default 90 days);
  the disabled map is never purged. Trigger: piggyback on the existing activity
  debounce (on write, at most once
  per hour) — no background task, idle broker does nothing. File purge =
  unlink day files older than the horizon (atomic, no rewrite, no race with
  concurrent appends). DB backends: one `DELETE WHERE called_at < cutoff`
  (Plan 3 puts it in the generic port). Delete the public `purge_calls` API.

## 2.8 Spec rewrite

Replace `specs/reference/optimizer.md` wholesale with a short spec (target: one
to two screens) stating the current rules only:

- cooldown: trust `Retry-After`, streak-scale by `backoff_factor**consecutive_fails`,
  cap at `max_delay`, flat 60s default base; dead key (401/403) → immediate drop
  + log line; shared across instances via the journal (failed rows carry
  `cooldown_until` and `key_hash`), refreshed by the debounced rebuild read
  and on own failures — no state store exists;
- scoping: registry and learning are global; the scope is an opaque string
  the broker turns into a secret-ref prefix (own key, falling back to the
  shared ref) and a journal attribution field — no typed user exists in
  storage or protocols; 429 cooldowns and dead-key drops follow the key hash,
  5xx cooldowns are global;
- learning: per `(model, operation)` window of the last `quality_window` ratings;
  demoted ⟺ ≥ `quality_min_count` ratings and Wilson upper bound <
  `quality_floor`; recovery via new ratings and last-resort traffic — no
  quality reset exists; no global verdict exists — demotion is always per
  (model, operation);
- derived state: the journal is strictly append-only (quality is its own
  record everywhere); one debounced tail read feeds score windows, shared
  cooldowns, and snapshot metrics, and re-reads the registry and disabled map so
  admin edits propagate to running brokers; persistent by default (`state/`),
  memory-only under the explicit opt-out; journal retention is the forgetting
  mechanism; the manual block is an admin verdict in the knowledge disabled
  map (values written only by `set_disabled` or by hand; llmbroker seeds
  missing names only; read via the `get(name)` handle and `snapshot()`);
  nothing but `sync` writes the registry;
- seeding: the preset file is the only source of model definitions; explicit
  `sync(preset)` mirrors it (cluster rationale), preserving `disabled`;
  no model CRUD; empty-registry fail-fast;
- ratings: a quality record is self-contained (llm, operation, score);
  `record_quality` takes them from the caller, `call_id` is an optional
  host-UI passthrough — no join with call rows exists;
- selection: one sort — demoted-for-this-operation last, otherwise curated
  order (2.2); parallel per-LLM up to `LLMConfig.parallel`;
- visibility: no alerts API — flip events are log lines; no status enum —
  `snapshot()` serves raw per-model facts (`disabled`, `has_key`,
  `cooldown_until`, `demoted_operations`) and metrics (zero DB reads — from
  the cached tail); the DB schema is not a public contract (hosts query `llmbroker_calls`
  at their own risk).

Update `architecture.md` cross-references (optimizer bullet, seeding, key help,
knowledge-store defaults, state-store description).

## 2.9 Tests

| File | Action |
|---|---|
| `tests/test_optimizer.py` (1040) | rewrite small: windowed Wilson verdicts per (name, op), per-op flips (log lines via `caplog`), backoff counters, dead-key drop |
| `tests/test_optimizer_integration.py` | rewrite: rating→demotion→demoted-last selection end-to-end; journal rebuild round-trip (sqlite **and file** knowledge stores); **two brokers sharing one journal converge on the same verdicts** — including brokers with different `scope` (learning is global); a 429 on the shared key cools peers on the shared key but not a broker holding an own (scope-prefixed) key; a dead own key (401/403) drops the model only for its scope; a scope without an own key falls back to the shared secret; registry edit from a second connection picked up by the debounced re-read |
| `tests/test_pool.py` | demoted-last sort (demoted for the operation goes last, curated order otherwise); demoted-only pool still serves |
| `tests/test_catalog.py` | drop IF_EMPTY/ADD/MIRROR and model-CRUD cases; mirror `sync`: new added, changed updated, absent deleted (replaces deprecation cases), `disabled` preserved across sync; identity-change refusal raises; empty-registry fail-fast |
| `tests/test_broker_bench.py` | keep, rename to `test_broker_disable.py`; disable as a knowledge disabled-map verdict via `set_disabled(name, flag)`; readable via `get(name).disabled` and the `disabled` snapshot field; survives restart with any persistent knowledge backend and **survives `sync`** (which rewrites only the registry); sync/provision seed missing names without touching values; a hand-edited `state/disabled.yml` and a second process's write are picked up by the debounced re-read |
| `tests/test_cli_env.py`, `test_registry_keys.py` | passthrough/file-order assertions |
| `tests/test_broker.py` | `snapshot()` raw fields + metrics; flip events appear in logs; default knowledge store in sibling `state/` dir |
| knowledge tests | jsonl record format (no null fields, `ts` present); day files under `state/calls/`; retention purge drops expired day files/rows and never touches the disabled map |

Delete tests of deleted mechanisms (exploration, floors, Wilson bands,
retirement, summaries folds, profile score snapshots) — they assert behavior that
no longer exists; the one sanctioned case of test deletion. Bug-repro tests tied
to surviving behavior are ported, never deleted.

---

## Step order

1. **2.1 + 2.2** windows + demoted-last sort (one step — they share the verdict seam)
2. **2.3** journal-derived rebuild + `_LearningHook` + the disabled map
3. **2.4** alerts-API removal + `snapshot()` raw facts
4. **2.5** mirror `sync`, model CRUD deleted
5. **2.6** key-info passthrough
6. **2.7** knowledge-store defaults + format + retention
7. **2.8** spec rewrite

**Plan gate:** `invoke pre` + full `python -m pytest` green, zero skips. Then
proceed to `specs/plans/simplify-storage.md`.

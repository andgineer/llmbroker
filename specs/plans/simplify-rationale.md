# Simplification rationale (2026-07)

Background discussion behind the three simplification plans:
`specs/plans/simplify-core.md` → `specs/plans/simplify-learning.md` →
`specs/plans/simplify-storage.md`. Requirements are captured in
[`specs/reference/mission.md`](../reference/mission.md); this document
records the decisions taken to satisfy them and the resulting cost estimate.
It does not modify the plans themselves.

## Decisions accepted (2026-07)

- **Learning comes from the call journal, not a separate subsystem.** The
  journal stores every score and is append-only — concurrent instances
  cannot overwrite each other, so verdict synchronization is correct by
  construction. Rolling score windows are recomputed from the journal on an
  activity debounce (60s); one's own score applies instantly. Learning
  durability comes from knowledge (enabled by default, see below); only an
  explicit in-memory mode leaves learning in the process's memory.
  Forgetting = the journal's automatic retention. No decayed aggregates,
  summaries table, atomic folds, or last-writer-wins snapshots.
- **Append-only journal, no exceptions**: a quality score is always a
  separate, **self-contained** record — it carries (model, operation, score)
  by itself, with no stitching to the call row during recomputation
  (`call_id` is an optional reference for the host UI; llmbroker never reads
  it). This fixes the loss of late scores: a score whose call has already
  fallen out of the journal's readable tail is still counted.
  `record_quality` accepts (model, operation, score) — the host takes these
  from the `ask()` result. There is no row UPDATE in any backend's journal;
  `set_field` is removed from the drivers. One mechanism instead of two, and
  the invariant "the journal is never mutated" is absolute.
- **Everything derived comes from one cached journal tail.** Score windows,
  other instances' cooldowns, and `snapshot()` metrics are all computed from
  the same read of the journal tail (60s debounce, plus an out-of-turn read
  on one's own failure) — three mechanisms collapse into one: "the journal
  is written; the tail is read on a debounce; everything else is a pure
  function over it." `metrics_rows` is removed from the protocol and
  drivers; `snapshot()` never hits the DB at all (metrics become "over the
  last N records" instead of "over all time"; with a 90-day retention the
  difference is cosmetic).
- **The registry and the disabled-doc are re-read on the same debounce**
  (4-5 rows plus a tiny document — negligible cost): edits from another
  process (a disable verdict, a key change, a new model after sync) are
  picked up on the fly, without a restart. Otherwise they would only be read
  at startup, and "zero administration" would be dishonest.
- **Statistics: window + Wilson**: demotion = "≥10 scores and Wilson upper
  bound < 0.3" (demote only when even the optimistic estimate sits below the
  floor) for a (model, operation) pair; the bound is computed over a
  window of the latest scores, aggregated from the journal. **Tiers are
  removed as a concept**: selection becomes a simple sort — "demoted for
  this operation go to the back, the rest keep curated order." A global
  verdict ("bad across everything measured") does not exist at all: demotion
  is always per (model, operation), `snapshot()` returns the list per model
  (the deprecated tier died earlier along with the field, see sync). The
  formula itself is ~30 lines and is free. What was expensive in the
  previous design was not the formula but the decayed aggregates underneath
  it: decay ("multiply, then add") is not expressible as an atomic
  increment, hence a summaries table and CAS-folds in each of the 4 backends
  (~500-600 lines). Without decay, the aggregate is plain counters and an
  atomic increment is cheap, but an append-only jsonl (the default journal)
  cannot be incremented in place — the "aggregate from rows" path has to
  exist anyway, so fold-in-place is not brought back even in its cheap form:
  there is one mechanism — separate rows plus a debounced recompute.
- **Model knowledge is an internal llmbroker subsystem, not logging**:
  everything llmbroker knows about models beyond the config is the
  **journal** (append-only experience: calls, scores, cooldown marks) plus
  the **disabled-doc** (manual admin verdicts; a tiny mutable "name → bool"
  document). The application logs through its own normal means; plain-text
  emission into python logging is removed from the package. The default for
  a TOML source is a `state/` directory next to the TOML (overridable via a
  parameter): journal at `state/calls/YYYY-MM-DD.jsonl`, verdicts at
  `state/disabled.yml` — YAML for convenient manual editing (PyYAML is an
  acceptable core dependency). The doc is **pre-populated with model names**
  (missing entries with `disabled: false` are appended on sync, and for a
  file source, at startup), so the admin only edits flag values; llmbroker
  itself never changes them. For a DB source: tables in the same DB
  (journal + disabled-doc); when `registry=` is passed explicitly with no
  knowledge backend, the default is `state/` in the CWD (not an error). A
  per-day journal file is a storage layout, not aggregation: rebuild needs
  raw scores, and scores arrive for calls from past days — daily aggregates
  would have to be constantly reopened. Row format: no null fields, with a
  timestamp. The journal cleans itself — default retention 3 months,
  changeable via an init parameter; for jsonl this means deleting whole old
  files; the disabled-doc is not subject to retention; there is no public
  purge command. Knowledge holds no derived data: the journal is raw
  material, verdicts are manual input, everything derivable is computed at
  read time.
- **The preset file alone determines the model list; sync is a total
  mirror.** The registry is a pure projection of the file: nothing but
  `sync` ever writes to it (admin verdicts live in the knowledge
  disabled-doc). Model CRUD (`add`/`remove`/`update`), the `origin` field,
  merge rules, and `copy_registry` do not exist — admin runtime actions
  (`set_disabled`, editing keys) never touch the registry; a host with its
  own models keeps its own file (a preset is "your model list," not
  necessarily a file from the repo). The optimizer and learning never write
  to the registry. The `seed=`/`seed_policy=` parameters are removed:
  implicit seed-on-start is unworkable in a cluster — every node would
  coerce the registry to its own local copy of the TOML, and diverging
  copies would flip-flop the registry. `sync` is called explicitly — a
  fresh preset was downloaded, initialize the app's DB; mirror semantics:
  add new entries, update existing ones, **delete ones that disappeared**
  (the `deprecated` field is dropped: there is nothing to lose — keys live
  separately in the secrets store, statistics are derived from the journal,
  the journal does not cascade; a model returning to the preset is simply
  re-added, and its old scores and verdicts are picked back up); sync also
  appends missing model names to the knowledge disabled-doc (`disabled:
  false`) without touching existing values; refusing to change a model's
  identity is a call error (protecting the binding between statistics and
  the model name), not an alert.
- **Manual blocking is an admin verdict in knowledge, not a registry
  field.** Demotion is soft (to the back of the queue; a last resort still
  gets traffic), and that is not enough for a truly useless model: hard
  exclusion is a manual "discarded" verdict. It lives in the disabled-doc of
  the knowledge subsystem (see above), not in the registry: it survives sync
  by construction (sync only touches the registry) and works identically
  for TOML and DB sources — no overlays next to the config. It changes via
  a single method `set_disabled(name, flag)` (not a disable/enable pair) or
  by hand-editing the yml/row; llmbroker itself only appends missing model
  names with `disabled: false`, and learning never writes anything to the
  doc (learning verdicts are always computed from the journal at read
  time). Current state is read via the `get(name)` handle (the `disabled`
  property) and `snapshot()` (the same `disabled` field). This is not a
  journal write: retention would silently erase the verdict.
  `set_disabled(name, False)` simply lifts the verdict; rehabilitation
  happens through new scores (the window of the latest N displaces the old
  ones), there is no separate "reset statistics." The term "bench" leaves
  the code and docs: manual blocking is simply called `disabled`.
- **Bandit machinery is removed**: ε-exploration, usable_rate floors,
  latency ranking, auto-retirement. A chronically failing model is
  effectively disabled by exponential cooldown (up to 1 hour); the only
  thing auto-removed is a dead key (401/403), logged with an error line.
- **The `alerts()` API is removed entirely** (the list, drain-on-read, the
  `Alert` model): the UI works by pulling — current state is visible from
  `snapshot()`'s raw fields (`disabled`, `has_key`, `cooldown_until`,
  `demoted_operations`); there is no status enum or priority rule — the UI
  chooses the presentation. Three important events (dead key, demotion,
  "everything is cooling down") remain `logger.warning`/`error` lines.
  Realert intervals and debounce maps are removed; refusing to change a
  model is a synchronous `sync` error.
- **Metrics stay in `snapshot()`** (`LLMMetrics`: count, last status,
  last-call time): this is the only stable API for reading the journal;
  computed from the cached journal tail, with no queries of its own. **The
  llmbroker table schema is not a public contract**: compatibility of the
  host's direct queries against `llmbroker_*` is not guaranteed or supported
  (a host may query `llmbroker_calls` at its own risk); typical statistics
  go through snapshot, anything more elaborate is of no use to anyone but
  llmbroker itself.
- **Selection order among non-demoted models is curated priority**: the
  best available model takes all traffic until it goes into cooldown from a
  429; a request that hits quota exhaustion transparently spills to the
  next one (the cost is one extra roundtrip at that moment; under high
  parallelism, a batch of simultaneous 429s). This is normal, not a
  problem: it maximizes answer quality. Round-robin is removed.
- **`KeyInfo` is a passthrough** of the TOML section. Help text and
  arbitrary fields from `[keys.*]` are preserved and printed by the `env`
  command as now — this information costs nothing and is not removed. Only
  the closed `EffortLevel`/`ValueLevel` dictionaries with their validation
  and sorting are removed: the saving is not in lines (~100 including
  tests) but in dropping a closed vocabulary that every preset section
  would have to conform to (an unknown value is a parse error, extending
  the vocabulary is a code release). Onboarding order is given by section
  order in the file — the preset is already curated.
- **No DB migrations**: a single installation (0.0.12), upgraded manually;
  `ensure_schema` creates from scratch, and on a version mismatch, fails
  fast with instructions.
- **Scopes: the registry and learning are always shared, per-user applies
  only to keys and their quotas.** Scope does not touch the registry (one
  model list, one `sync`; the user parameter leaves the registry protocol)
  or learning (score windows, verdicts, and metrics are aggregated over the
  whole journal — the quality signal is not fragmented per user). What
  remains: secrets scope (a personal key; absent, it falls back to the
  shared key; the fallback policy lives in the broker, secret lookups stay
  exact) and journal-row attribution (a scope field in the row, a filter in
  `calls()`); the scope mechanism is a string prefix, see the next point.
  Quota failures follow the key, not the user: a failing 429/401/403 row
  carries a short hash of the key actually used, and another instance's
  cooldown/dead-key state applies only when the hash matches the current
  instance's key — a shared key gets a shared cooldown, a personal key gets
  a personal one, and identical key values (env secrets, duplicates) merge
  into one quota scope by themselves. 5xx/timeouts are provider-level: the
  cooldown is unconditionally shared.
- **Scope is an opaque string; a typed `user_id` does not exist.** The
  broker accepts `scope: str | None` (e.g. `"user/42"`); storage and
  protocols know nothing about users. Secrets: the protocol narrows to
  `resolve(ref)` — the broker itself builds the prefixed name
  `{scope}/{ref}` and, on a miss, falls back to the plain `ref` (the
  "personal → shared" fallback is written once, in the broker); the
  secrets backend is a flat key-value store with no second dimension: no
  `user_id` columns, no `IS NULL` semantics, no per-backend "ref + user"
  path/key stitching. For env secrets, the personal prefix simply won't be
  found and the fallback to the shared key kicks in — the same behavior as
  today. Journal: the call row carries `scope` as an ordinary string field
  (attribution, a filter in `calls()`), llmbroker never interprets it.
  `check_user_id`, `require_user_id=`, the `UserIdRequired` exception, and
  the `int | str | None` union all disappear from the code; as a side
  effect, the `42` vs `"42"` collision goes away — scope is always a
  string. Quota scoping does not depend on any of this: it already follows
  the hash of the key's value.
- **Shared cooldown comes from the journal; there is no state store.** A
  failing journal row (429/503) is already written — it gains a
  `cooldown_until` field (computed by the instance that caught the failure,
  from `Retry-After`/backoff). Other instances' cooldowns = max
  (`cooldown_until`) over recent failing rows in one's own scope (5xx — all
  rows, 429 — matching key hash, see the scope point above), and they are
  read by **the same** journal-tail read as the score windows (60s
  debounce), plus an out-of-turn read on one's own failure — coordination
  is only needed around failures. There is no separate TTL/2s cache in the
  system. Coordination is advisory: correctness is provided by failover,
  the cost of staleness is one wasted roundtrip with a transparent
  spillover; a stateless process starts informed (first journal read). The
  shared fail-streak is the count of recent failing rows. The state-store
  protocol, port, table, reconcile, and the Redis backend (along with its
  extra and fakeredis) are removed.
- **Stacks are removed — the data source is given by a single parameter**:
  `Broker("config.toml")` / `Broker("llm.db")` / `Broker("postgresql://…")`
  / `Broker("mongodb://…")` — recognized by scheme/extension (`.toml`,
  `.db`/`sqlite://`, otherwise a clear error), the driver is imported
  lazily. Registry + knowledge + secrets are all derived from the source;
  `secrets=` / `knowledge=` / `registry=` remain for mixed configurations
  (Vault, in-memory, etc.). The `Stack` classes, `BackendStack`, and the
  `stack=` parameter go away. The saving is conceptual, not in lines
  (~zero).
- **Storage layer**: one narrow per-DB `Driver` protocol plus generic ports
  (registry, knowledge, secrets), written once. Behavior tests are written
  once; backends are covered via parameterized fixtures and a conformance
  suite, with no test duplication per backend.
- **Core**: a slot table instead of `asyncio.Queue` (no `call_later` timers
  or loop-bound state), per-LLM parallelism by default.
- **The sync wrapper stays on a background thread** (decided to keep it,
  not simplify down to `asyncio.Runner`): `Runner` executes the coroutine
  on the calling thread, and calls from multiple threads would have to be
  serialized with a mutex — parallel LLM calls from a multi-threaded
  synchronous host (e.g. Flask with a thread pool) would queue up. A
  background loop gives N threads honest parallelism; ~120 extra lines is
  the price of that scenario.
- **The set of batteries stays as-is** (decided to keep it): sqlite,
  postgres, mongodb, aws/vault; redis is dropped along with the state
  store. Each DB driver is ~160 lines behind one generic port — there is no
  reason to shrink the set.
- **`RateLimit` is removed**: `rpm`/`rpd`/`tpm`/`tpd` are not enforced
  anywhere (only the daily-cap annotation in `env` — dies with them). The
  per-LLM concurrency cap `parallel` is a **new** plain field of `LLMConfig`
  (TOML: `parallel = 1`), not a survivor of `RateLimit` — no such field
  exists today.

## Function → mechanism → cost

Line estimates are after all three plans are executed (current `src` ≈
6000).

| Function | Requirement | Mechanism after simplification | ~lines | DB per call |
|---|---|---|---|---|
| Routing, cooldown, failover, parallelism | 1, 8 | slot table + Condition; cooldown = timestamp; backoff by streak; curated priority | 500 (pool+router) | 0 |
| Broker facade, lifecycle, per-user keys, `sync()` | 2, 4, 6 | AsyncBroker + `_LearningHook`; personal→shared key fallback | 330 | — |
| Learning per (model, op) + selection order | 2, 3 | score windows, Wilson bound; demoted-for-operation go to the back; verdicts from the journal tail on a 60s debounce | 170 (optimizer) | shared tail read / 60s of activity |
| Knowledge: journal + disabled-doc | 5, 3 | insert per call; a score is a separate record everywhere (the journal is strictly append-only); verdicts — a tiny "name → bool" doc; automatic 3-month journal retention | in the ports | 1 insert |
| Cluster shared cooldown | 6 | from the journal: a failing row carries `cooldown_until`; same tail read + on one's own failure | ~0 (in rebuild) | 0 extra |
| Picking up admin/cluster edits | 2, 4 | registry and disabled-doc re-read on the same debounce | ~10 | 1 tiny read / 60s of activity |
| Explicit `sync(preset)` + secrets bootstrap | 2 | total mirror of the preset: add/update/**delete**; changing `model` identity is an error | 80 (catalog) | on call |
| Snapshot: raw fields + metrics | 5 | pull via `snapshot()`: `disabled`, `has_key`, `cooldown_until`, `demoted_operations`; metrics from the cached tail; events go to the log | 50 | 0 |
| Sync wrapper | 6 | background loop thread, direct construction | 200 | — |
| Models/protocols/exceptions | — | dataclasses + to/from dict | 350 | — |
| Standalone (TOML/env/`state/`) | 6 | default: per-day journal in `state/calls/`, verdicts in `state/disabled.yml` (pre-populated with names), retention by deleting files | 330 | — |
| Driver layer: spec + protocol + generic ports + in-memory | 7 | boilerplate (scope, schema, JSON, errors) written once; no `set_field`/`metrics_rows` | 430 | — |
| Drivers: sqlite, postgres, mongodb | 7 | one file of "plain queries" each, ~150 lines each | 450 | — |
| AWS / Vault secrets | 4, 7 | SDK glue, as now | 150 | — |
| CLI (`env` passthrough, `preset`, `sync`) | 2 | print in file order; sync for DB init | 130 | — |
| chat.py (provider call) | 1 | unchanged | 200 | — |
| **Total** | | | **≈3100-3400** | **1 insert per call; every 60s of activity — journal tail + registry; 0 while idle** |

## What was dropped (and why it isn't a loss of functionality)

- **Summaries subsystem** (~500-600: table, atomic folds in 4 backends,
  warm-start, delta batching) and **decayed aggregates** — Wilson over a
  journal window gives the same verdicts; the journal is already a
  consistent shared store of scores.
- **Bandit machinery**: ε-exploration, usable_rate floors, latency ranking,
  auto-retirement (duplicates exponential cooldown), `SelectionPolicy` /
  `policy=`.
- **`SeedPolicy` and the implicit `seed=`** — replaced by an explicit
  `sync(preset)`.
- **The `deprecated` field and its tier** — sync deletes preset entries
  that disappeared: there is nothing to lose (keys live in the secrets
  store, statistics are derived from the journal).
- **`LLMProfile` as state that llmbroker itself wrote** (a registry profile
  column with learning snapshots, `write_profile`/`read_profiles`) —
  manual blocking is an admin verdict in the knowledge disabled-doc;
  learning writes nowhere.
- **`quality_reset_at` / `reset_quality`** — rehabilitation happens through
  new scores, the window displaces the old ones.
- **The state store entirely** (protocol, port, `llmbroker_state` table,
  reconcile, 2s cache) — shared cooldown is derived from the journal: a
  failing row is already written, the state store duplicated that record.
- **The Redis backend**, along with its extra and fakeredis — its only
  role was the state store.
- **Stacks** (`Stack` classes, `BackendStack`, `stack=`) — the data source
  is given by a single parameter.
- **Plain-text `Telemetry`** — knowledge is not logging; JSON is
  preferable, including for CloudWatch.
- **Public `purge_calls`** — retention is automatic.
- **The effort/value taxonomies** — passthrough plus section order in the
  file.
- **Round-robin (`pick_seq`)** — curated priority.
- **The `alerts()` API** with the `Alert` model, debounce maps, and
  realert intervals — events are written to the log, state is visible in
  `snapshot()`.
- **`RateLimit`** with unenforced `rpm`/`rpd`/`tpm`/`tpd` and the daily-cap
  annotation in `env` — the `LLMConfig.parallel` field remains.
- **Tiers as a concept** — selection became a sort ("demoted for this
  operation go to the back"); no global verdict exists.
- **`set_field` and `metrics_rows` from the drivers** — a score is written
  as a separate record, metrics are computed from the cached journal tail.
- **`asyncio.Queue` + `call_later` timers** — slot table + Condition.
- **Schema migrations** — create-if-missing + fail-fast on version
  mismatch.
- **The typed `user_id` entirely** (along with `check_user_id`,
  `require_user_id=`, the `UserIdRequired` exception, and per-user scopes
  for the registry and learning) — scope is an opaque string: a
  key-reference prefix plus journal-row attribution; the registry and
  learning are always shared; quota follows the key (a key hash in failing
  rows).
- **Model CRUD, `origin`, and `copy_registry`** — the model list is
  determined only by the preset file, sync is a mirror that preserves
  `disabled`; the admin's runtime verbs are disable/enable and keys.
- **Stitching quality records to call rows via `call_id`** — a score is
  self-contained (model, operation, score); as a side effect, scores whose
  calls have already fallen out of the readable journal tail stop being
  lost.

No candidates for further savings remain — all have been considered and
closed by the decisions above (what was dropped stays dropped; the sync
wrapper, the set of batteries, snapshot metrics, and slot waiting
(`wait=`/`Condition` in the pool) were decided to be kept).

No open questions remain: a string scope instead of a typed `user_id` —
decided (see the scope point above). A strictly read-only file registry —
decided: nobody writes the config (including llmbroker itself), admin
verdicts live in the knowledge disabled-doc, no sibling-JSON overlay
exists.

The public signature `ask(prompt, operation=, trace_id=, wait=)` does not
change — `operation` is optional (decided): scores for calls without a
label accumulate in a separate (model, None) bucket; `trace_id`/`wait` stay
as they are. `record_quality` accepts (model, operation, score) and an
optional `call_id` — a pass-through reference for the host UI's journal.

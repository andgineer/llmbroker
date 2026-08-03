# Design decisions and cost rationale (2026-07)

Requirements are captured in [`mission.md`](mission.md); this document records
the decisions taken to satisfy them during the 2026-07 simplification and the
resulting cost estimate. The current behavior rules themselves live in
[`architecture.md`](architecture.md) and [`optimizer.md`](optimizer.md).

## Decisions accepted (2026-07)

- **Learning comes from the call journal, not a separate subsystem.** The
  journal stores every score and is append-only — concurrent instances
  cannot overwrite each other, so verdict synchronization is correct by
  construction. Rolling score windows are recomputed from the journal on an
  activity debounce (60s); one's own score applies instantly. Learning
  durability comes from the store (enabled by default, see below); only an
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
  from the `ask()` result. There is no row UPDATE in any backend's journal —
  inserts only. One mechanism instead of two, and the invariant "the journal
  is never mutated" is absolute.
- **Everything derived comes from one cached journal tail.** Score windows,
  other instances' cooldowns, and `snapshot()` metrics are all computed from
  the same read of the journal tail (60s debounce, plus an out-of-turn read
  on one's own failure) — three mechanisms collapse into one: "the journal
  is written; the tail is read on a debounce; everything else is a pure
  function over it." `snapshot()` never hits the DB at all (metrics become
  "over the last N records" instead of "over all time"; with a 90-day
  retention the difference is cosmetic).
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
  is always per (model, operation), and `snapshot()` returns the list per
  model. The formula itself is ~30 lines and is free. What was expensive in the
  previous design was not the formula but the decayed aggregates underneath
  it: decay ("multiply, then add") is not expressible as an atomic
  increment, hence a summaries table and CAS-folds in each of the 4 backends
  (~500-600 lines). Without decay, the aggregate is plain counters and an
  atomic increment is cheap, but an append-only jsonl (the default journal)
  cannot be incremented in place — the "aggregate from rows" path has to
  exist anyway, so fold-in-place is not brought back even in its cheap form:
  there is one mechanism — separate rows plus a debounced recompute.
- **The store is an internal llmbroker subsystem, not logging**:
  everything llmbroker knows about models beyond the config is the
  **journal** (append-only experience: calls, scores, cooldown marks) plus
  the **disabled-doc** (manual admin verdicts; a tiny mutable "name → bool"
  document). The application logs through its own normal means; plain-text
  emission into python logging is removed from the package. The default for
  a TOML source is a `store/` directory next to the TOML (overridable via a
  parameter): journal at `store/calls/YYYY-MM-DD.jsonl`, verdicts at
  `store/disabled.yml` — YAML for convenient manual editing (PyYAML is an
  acceptable core dependency). The doc is **pre-populated with model names**
  (missing entries with `disabled: false` are appended on sync, and for a
  file source, at startup), so the admin only edits flag values; llmbroker
  itself never changes them. For a DB source: tables in the same DB
  (journal + disabled-doc); when `registry=` is passed explicitly with no
  store backend, the default is `store/` in the CWD (not an error). A
  per-day journal file is a storage layout, not aggregation: rebuild needs
  raw scores, and scores arrive for calls from past days — daily aggregates
  would have to be constantly reopened. Row format: no null fields, with a
  timestamp. The journal cleans itself — default retention 3 months,
  changeable via an init parameter; for jsonl this means deleting whole old
  files; the disabled-doc is not subject to retention; there is no public
  purge command. The store holds no derived data: the journal is raw
  material, verdicts are manual input, everything derivable is computed at
  read time.
- **The lineup determines the model list; `sync` is the only path that
  writes it.** The registry is a projection of the arriving lineup merged
  with what is already there: nothing but `sync` ever writes to it (admin
  verdicts live in the store disabled-doc); there is no separate CRUD path
  or registry cloning — admin runtime actions (`set_disabled`, editing
  keys) never touch the registry; a host with its own models keeps its own
  lineup (a preset is "your model list," not necessarily a file from the
  repo). The optimizer and learning never write to the registry. What a
  node must never do is coerce the shared registry to a **local copy** of
  its own: diverging copies would flip-flop it in a cluster. An implicit
  refresh follows the one shared upstream instead, so nodes converge — the
  premise being that one registry means one secrets store, so every node
  resolves the same keys and computes the same merge. The merge itself
  adds new entries and updates existing ones, and removes a dropped one
  only under the rule in [`architecture.md`](architecture.md) (same
  provider replaces it, no key for it here, or the journal proves it
  dead) — nothing is lost by a removal that does happen: keys live in the
  secrets store, statistics derive from the journal, and a model that
  returns is re-added with its old scores and verdicts. `sync` also
  appends missing model names to the store disabled-doc (`disabled:
  false`) without touching existing values; refusing to change a model's
  identity is a call error (protecting the binding between statistics and
  the model name), not an alert.
- **The curated lineup keeps itself current, unconditionally.** A pinned
  free-tier lineup is a decaying one — providers retire free endpoints
  without notice — so a broker following the curated preset re-checks it
  on an interval, gated by time (one monotonic comparison per call, one
  small GET per node per day) and by identity (zero writes and no log line
  when the merged result is what is already stored). The check is lazy on
  activity rather than timed, so an idle process performs no I/O and the
  library still needs no service of its own. There is no off switch: what
  an off switch appears to protect against — an unreviewed lineup change —
  is already bounded by the removal rule, and what it buys is a pool that
  decays to nothing. The cost accepted with it: the catalog's default
  branch is live configuration everywhere. Pinning the fetch to the
  installed version's tag would close that and is rejected — a preset fix
  would then reach nobody until a release of llmbroker, which is the
  problem the refresh exists to remove. A fetched lineup must carry
  `https://` endpoints, which removes plaintext key transmission as an
  accident without pretending to defend against a compromised catalog.
- **Manual blocking is an admin verdict in the store, not a registry
  field.** Demotion is soft (to the back of the queue; a last resort still
  gets traffic), and that is not enough for a truly useless model: hard
  exclusion is a manual "discarded" verdict. It lives in the disabled-doc of
  the store subsystem (see above), not in the registry: it survives sync
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
  ones), there is no separate "reset statistics."
- **Bandit machinery is removed**: ε-exploration, usable_rate floors,
  latency ranking, auto-retirement. A chronically failing model is
  effectively disabled by exponential cooldown (up to 1 hour); the only
  thing auto-removed is a dead key (401/403), logged with an error line.
- **A client request error is the caller's fault, not the model's — it never
  cools anything.** Cooldown exists to stop hammering a model that is
  unavailable (quota, auth, provider 5xx/timeout); a 4xx that is not
  429/401/403 means the request itself was rejected, so the same call fails
  over to the next model and excludes the offender for the rest of that call
  only — a later request may use it again immediately, and its failure never
  counts toward a cooldown streak. When every candidate rejects the request
  this way, the caller gets the provider's own error rather than a generic
  "no model available": the fault is in the request and only the provider
  error is actionable.
- **"No model available" is one error carrying a machine-readable reason, and
  `wait` is taken literally.** A single error class distinguishes its causes
  (empty pool, no resolved key, all disabled, every candidate excluded for
  the call, or a genuine timeout) and, when the pool is only temporarily
  exhausted, carries the earliest time a model is expected back. `wait` bounds
  the whole call, not each internal acquire: `wait=0` is non-blocking yet
  still spills across models free right now; the default `wait=None` waits
  exactly as long as some model can still return by itself (a cooldown
  expiring, a busy slot releasing) and raises the instant nothing ever can —
  when the pool is empty, keyless, fully disabled, or fully excluded there is
  no event that would ever wake a waiter, so blocking there would be a silent
  hang rather than "wait as long as needed."
- **There is no alerts/events API**: the UI works by pulling — current state
  is visible from `snapshot()`'s raw fields (`disabled`, `has_key`,
  `cooldown_until`, `demoted_operations`) plus the pool-wide provider counts
  and `degraded` predicate on the same object; there is no status enum or
  priority rule — the UI chooses the presentation. The important events
  (dead key, demotion, "everything is cooling down", a pool degraded to one
  quota or none) are `logger.warning`/`error` lines instead; refusing to
  change a model is a synchronous `sync` error.
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
  command as-is. There is no closed effort/value vocabulary a preset
  section has to conform to (an unknown value would otherwise be a parse
  error, and extending the vocabulary would require a code release).
  Onboarding order is given by section order in the file — the preset is
  already curated.
- **No DB migrations**: a single installation (0.0.12), upgraded manually;
  `ensure_schema` creates from scratch, and on a version mismatch, fails
  fast with instructions.
- **Every backend keeps its schema marker inside its own `llmbroker_`-namespaced
  object**, so dropping the `llmbroker_*` objects fully resets llmbroker's state —
  which is what the mismatch error tells the operator to do, and it has to be
  enough. Nothing outside that namespace is llmbroker's to write: on SQLite the
  file header (`PRAGMA user_version`) belongs to the embedding application, which
  is the whole database in the documented shared-file setup. A database left by a
  release that stamped the header is adopted once — the marker object is created
  from that value and the header handed back — and a header found with no
  `llmbroker_*` tables in the file is the host's, never read and never cleared.
- **The broker never manages SQLite's `journal_mode`; WAL is the file owner's
  responsibility.** The SQLite driver does not enable WAL and does not set
  `busy_timeout` (sqlite3's 5 s default stands, and it is not exposed as a
  knob). Journal mode
  is a persistent, file-level property belonging to whoever owns the database
  file: on a database shared with the host application (the normal setup) the
  host owns it; on a file given to the broker alone the operator sets it once,
  out of band. The driver receives only a path and cannot tell the two apart, so
  enabling WAL unconditionally during schema setup would silently flip a shared
  file's mode — which is why it does not. **Future edits must not add a `PRAGMA
  journal_mode=WAL` or a configurable `busy_timeout` to the SQLite driver.**
  Cross-process schema DDL already serializes via `BEGIN IMMEDIATE`, covered by
  the default busy timeout; users are pointed at the shared-vs-separate-file
  choice in the server/cluster guide.
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
- **Scope is an opaque string; there is no typed `user_id`.** The broker
  accepts `scope: str | None` (e.g. `"user/42"`); storage and protocols
  know nothing about users. Secrets: the protocol narrows to `resolve(ref)`
  — the broker itself builds the prefixed name `{scope}/{ref}` and, on a
  miss, falls back to the plain `ref` (the "personal → shared" fallback is
  written once, in the broker); the secrets backend is a flat key-value
  store with no second dimension: no `user_id` columns, no `IS NULL`
  semantics, no per-backend "ref + user" path/key stitching. For env
  secrets, the personal prefix simply won't be found and the fallback to
  the shared key kicks in. Journal: the call row carries `scope` as an
  ordinary string field (attribution, a filter in `calls()`), llmbroker
  never interprets it. A string-only scope also avoids a `42` vs `"42"`
  collision. Quota scoping does not depend on any of this: it already
  follows the hash of the key's value.
- **Shared cooldown comes from the journal; there is no state store.** A
  failing journal row (429/503) is already written — it gains a
  `cooldown_until` field (computed by the instance that caught the failure,
  from `Retry-After`/backoff). Other instances' cooldowns = max
  (`cooldown_until`) over recent failing rows in one's own scope (5xx — all
  rows, 429 — matching key hash, see the scope point above), and they are
  read by **the same** journal-tail read as the score windows (60s
  debounce), plus an out-of-turn read on one's own failure that cooled the
  model or dropped it — coordination is only needed around failures that
  changed shared state, and a failure that changed none (a client-side 4xx,
  a spent `wait` budget) waits for the debounce like everything else. There is no separate TTL/2s cache in the
  system. Coordination is advisory: correctness is provided by failover,
  the cost of staleness is one wasted roundtrip with a transparent
  spillover; a stateless process starts informed (first journal read). The
  shared fail-streak is the count of recent failing rows. There is no
  separate state-store subsystem or Redis backend.
- **The data source is given by a single parameter, and no parameter is one
  of the forms**: `Broker()` / `Broker("config.toml")` / `Broker("llm.db")` /
  `Broker("postgresql://…")` / `Broker("mongodb://…")` — recognized by
  scheme/extension (`.toml`, `.db`/`sqlite://`, otherwise a clear error), the
  driver is imported lazily. Registry + store + secrets are all derived from
  the source; `secrets=` / `store=` / `registry=` remain for mixed
  configurations (Vault, in-memory, etc.). The saving is conceptual, not in
  lines (~zero).
- **The zero-config broker is the default, and the config file is for the
  people it is actually for.** The file used to be mandatory for everyone
  while carrying a decision for almost nobody: materializing the curated
  preset by hand is a ritual with no choice in it, and now that the lineup
  refreshes itself there is nothing in the copy to maintain either — asking
  the user to keep one is asking them to hold our state. So `Broker()` runs
  the curated pool out of the home directory, and a file stays the right
  answer for a lineup a team wants under version control and reviewable in a
  pull request. Rejected with it: **selecting curated pool models**
  (`models=[…]`), because free-tier entry names carry the model version and
  are rewritten on every bump, so selection by name would need a permanent
  per-entry handle the preset does not have and the curator would have to
  guarantee forever — and the case is thin, since a model with no key is
  already inactive and never routed.
- **Nothing a user declares enters the pool, and declared models are resolved
  rather than stored.** The pool is exactly the curated lineup: what it sells
  is failover across interchangeable free providers, and a self-hosted
  endpoint or company gateway dropped into it would be spilled onto by a 429
  it has nothing to do with. So pool membership is not a field — it is "not
  the host's own" — and a field that is always another field's negation is
  removed rather than kept as a way to disagree with yourself later. A model
  declared in code is overlaid on the registry at provision and re-resolved
  from the paid catalog every time, never written: storing it would create
  two sources of truth for one list (the constructor call and the stored row)
  and re-introduce exactly the drift alias-following exists to prevent.
- **Storage layer**: one narrow per-DB `Driver` protocol plus generic ports
  (registry, store, secrets), written once. Behavior tests are written
  once; backends are covered via parameterized fixtures and a conformance
  suite, with no test duplication per backend.
- **Core**: a slot table with an `asyncio.Condition`, no loop-bound timer
  state; per-LLM parallelism by default.
- **The sync wrapper stays on a background thread** (decided to keep it,
  not simplify down to `asyncio.Runner`): `Runner` executes the coroutine
  on the calling thread, and calls from multiple threads would have to be
  serialized with a mutex — parallel LLM calls from a multi-threaded
  synchronous host (e.g. Flask with a thread pool) would queue up. A
  background loop gives N threads honest parallelism; ~120 extra lines is
  the price of that scenario.
- **The set of batteries stays as-is** (decided to keep it): sqlite,
  postgres, mongodb, aws/vault — no redis. Each DB driver is ~160 lines
  behind one generic port — there is no reason to shrink the set.
- **No rate limits are enforced**: request/token caps (per-minute, per-day)
  are not tracked anywhere. The per-LLM concurrency cap `parallel` is a
  plain field of `LLMConfig` (TOML: `parallel = 1`) that serializes calls
  to one model rather than throttling by rate.
- **Journal aggregates are derived per request, never accumulated.** A sliding
  window ("the last 7 days") cannot be served by a monotonic counter: old calls
  must fall out of the aggregate on their own. Doing it with counters means day
  buckets plus rotation and subtraction — a second piece of stored state, its own
  ageing logic, and an atomic UPDATE on the hot path of every model call — while
  a time-bounded read over the indexed `called_at` is one cheap query. It would
  also contradict the rule above that everything llmbroker learns beyond config
  is re-derived from the journal: stored counters are exactly that second
  subsystem, and they must eventually disagree with the journal — on restart,
  after retention purges, and across nodes writing to one journal. If read volume
  ever justifies it the answer is a TTL cache over the aggregate, not counters:
  the same saving with an honest expiry. The filter belongs in the store (it is
  what makes the window exact and keeps the row count proportional to the window
  rather than to the row limit); the aggregation stays in shared Python, so one
  implementation serves all backends instead of a `GROUP BY` primitive
  reimplemented in each driver for an input the window has already bounded.
- **The library returns per-status counts; failure policy belongs to the host.**
  llmbroker does not decide what counts as a failure, how long a window should
  be, or how a model with no calls in the window should read. Baking a
  "failure rate" in would repeat the `call_count` mistake: a number whose meaning
  is fixed by the library and wrong for the next consumer. The aggregate carries
  only statuses actually observed, so "how many were not OK" is a subtraction
  rather than an assumption about the status enum's shape.
- **Every failure state a host is expected to handle has its own exception
  type.** A host that must tell two conditions apart by matching on message
  text has no contract at all — and the two lifecycle failures raised on the
  same call paths (an empty registry, benign and expected; a schema version
  this release cannot use, fatal and operator-actionable) are exactly that
  case: catching them together means reporting "not configured yet" when the
  store is unusable. Lifecycle failures form their own tree rooted at
  `RuntimeError`, separate from the request-error tree rooted at `Exception`:
  the two are different axes, and rooting lifecycle errors at `RuntimeError`
  keeps hosts that catch it around provisioning working unchanged. A fatal
  condition carries the facts a host would otherwise parse out of the message
  as attributes. The top-level package exports the lifecycle base and the
  benign, application-handled member of the tree; an operator-actionable
  deployment failure stays reachable through `llmbroker.exceptions` without
  being promoted onto the package surface, which is reserved for what an
  application is expected to catch in normal operation.

## Function → mechanism → cost

Line estimates for the design as built (pre-simplification `src` ≈ 6000).

| Function | Requirement | Mechanism after simplification | ~lines | DB per call |
|---|---|---|---|---|
| Routing, cooldown, failover, parallelism | 1, 8 | slot table + Condition; cooldown = timestamp; backoff by streak; curated priority | 500 (pool+router) | 0 |
| Broker facade, lifecycle, per-user keys, `sync()` | 2, 4, 6 | AsyncBroker + `_LearningHook`; personal→shared key fallback | 330 | — |
| Learning per (model, op) + selection order | 2, 3 | score windows, Wilson bound; demoted-for-operation go to the back; verdicts from the journal tail on a 60s debounce | 170 (optimizer) | shared tail read / 60s of activity |
| Store: journal + disabled-doc | 5, 3 | insert per call; a score is a separate record everywhere (the journal is strictly append-only); verdicts — a tiny "name → bool" doc; automatic 3-month journal retention | in the ports | 1 insert |
| Cluster shared cooldown | 6 | from the journal: a failing row carries `cooldown_until`; same tail read + on one's own failure | ~0 (in rebuild) | 0 extra |
| Picking up admin/cluster edits | 2, 4 | registry and disabled-doc re-read on the same debounce; the key table only when a managed ref has no key | ~10 | 1 tiny read / 60s of activity, 2 while a key is missing |
| Explicit `sync(source)` + secrets bootstrap | 2 | add/update, and a removal only when the same provider replaces it, no key for it exists here, or the journal proves it dead; changing `model` identity is an error | 80 (catalog) | on call |
| Merge engine + `SyncReport` | 2, 5 | provider-unit retirement with journal evidence, key-presence probe, file/registry writers; raw facts back to the caller and one log line per run | 400 (upstream) | 0 outside an explicit sync; one bounded journal read only when a provider was retired |
| Snapshot: raw fields + metrics + pool health | 5 | pull via `snapshot()`: `disabled`, `has_key`, `cooldown_until`, `demoted_operations`; metrics from the cached tail; provider counts and missing keys from the last reconcile, sharing the `degraded` predicate with the alarm; events go to the log | 60 | 0 |
| Merge report | 2, 5 | raw facts the host renders: what moved, which entries were kept and why, which retirement the journal justified and on what evidence, which key has lost its last user | 120 (models) | 0 |
| Sync wrapper | 6 | background loop thread, direct construction | 200 | — |
| Models/protocols/exceptions | — | dataclasses + to/from dict | 350 | — |
| Standalone (TOML/env/`store/`) | 6 | default: per-day journal in `store/calls/`, verdicts in `store/disabled.yml` (pre-populated with names), retention by deleting files | 330 | — |
| Driver layer: spec + protocol + generic ports + in-memory | 7 | boilerplate (scope, schema, JSON, errors) written once; no in-place row updates or dedicated metrics queries | 430 | — |
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
  auto-retirement (duplicates exponential cooldown).
- **Seeding the registry from a node's own local copy** — a node never
  coerces the shared registry to a copy of its own; the refresh follows the
  one shared upstream, and an explicit `sync(source)` is what mirrors a
  vendored file into a database registry.
- **A deprecation-tier field** — an entry a lineup drops is either removed
  (and nothing is lost: keys live in the secrets store, statistics derive
  from the journal) or kept as it is, routing exactly as before. There is no
  third, demoted state to represent.
- **A registry-stored learning profile that llmbroker itself wrote** (a
  profile column with learning snapshots) — manual blocking is an admin
  verdict in the store disabled-doc; learning writes nowhere.
- **An explicit quality-reset operation** — rehabilitation happens through
  new scores, the window displaces the old ones.
- **An LLM-as-judge scoring the pool's own replies.** Quality ratings stay
  host-supplied. A host that cares about quality already holds a better signal
  than a judge could infer — whether the JSON parsed, whether extraction
  validated, whether the user accepted the answer — and `record_quality()` is
  public for exactly that. A judge is a proxy for a signal the host usually
  has, and a weaker model judging a stronger one's reply is a poor proxy.
  It is also unaffordable where it would be needed: quality windows are
  per `(model, operation)` and need ten ratings apiece, so a small pool
  sampling a fraction of its traffic reaches a verdict slower than its free
  models are retired or delisted upstream — and every judge call spends the
  scarce quota the pool exists to conserve. Curated per-entry weights order
  the pool without any of that.
- **The state store entirely** (protocol, port, a dedicated table,
  reconcile, a short-TTL cache) — shared cooldown is derived from the
  journal: a failing row is already written, the state store duplicated
  that record.
- **The Redis backend**, along with its extra and fakeredis — its only
  role was the state store.
- **A dedicated backend-bundling layer** — the data source is given by a
  single parameter.
- **Plain-text log-style emission of store events** — the store is not
  logging; JSON is preferable, including for CloudWatch.
- **A public manual-purge operation** — retention is automatic.
- **The effort/value taxonomies** — passthrough plus section order in the
  file.
- **Round-robin selection** — curated priority.
- **A pull-drain events/alerts API**, with debounce maps and realert
  intervals — events are written to the log, state is visible in
  `snapshot()`.
- **Unenforced request/token rate limits** (per-minute, per-day) and the
  daily-cap annotation in `env` — the per-LLM `parallel` concurrency cap
  remains.
- **Tiers as a concept** — selection became a sort ("demoted for this
  operation go to the back"); no global verdict exists.
- **In-place row updates and dedicated metrics queries in the drivers** —
  a score is written as a separate record, metrics are computed from the
  cached journal tail.
- **A queue-plus-timer scheduling model** — slot table + Condition.
- **Schema migrations** — create-if-missing + fail-fast on version
  mismatch.
- **A typed `user_id` entirely** (along with its validation helpers and
  per-user scopes for the registry and learning) — scope is an opaque
  string: a key-reference prefix plus journal-row attribution; the
  registry and learning are always shared; quota follows the key (a key
  hash in failing rows).
- **Direct model CRUD** — the model list is determined by the lineup a sync
  merges in, and `disabled` is preserved across it; the admin's runtime verbs
  are disable/enable and keys.
- **Stitching quality records to call rows via `call_id`** — a score is
  self-contained (model, operation, score); as a side effect, scores whose
  calls have already fallen out of the readable journal tail stop being
  lost.
- **A `pool = false` marker on a key** (or any TOML field declaring "this key
  is not for the pool") — the state it describes is derivable, and the case
  it was invented for (a key kept for paid direct calls) is served by the
  unused-key report line plus death evidence.
- **A key-deletion path in the secrets protocol** — a key that cannot be
  deleted, because it still pays for direct calls, is the common case, so
  deletion could never be the retirement mechanism. llmbroker reports the
  orphaned ref and a human decides.
- **`sync(..., exact=True)`** — indistinguishable from `registry.mirror(configs)`,
  which already exists as the escape hatch for a forced lineup.
- **"Two callable providers" as a pruning threshold** — a policy constant that
  would discard working free quota. The same number survives only as the
  *degradation* criterion, where it describes the failover feature rather than
  deleting anything.
- **Probing the provider (`GET {base_url}/models`) for death evidence** —
  deferred; the journal is the evidence. Revisit only if it proves too thin.
- **An `llm_name` filter on the journal's `calls` query** — the bounded tail is
  enough for a handful of candidates, and a filter would touch every backend.

No candidates for further savings remain — all have been considered and
closed by the decisions above (what was dropped stays dropped; the sync
wrapper, the set of batteries, snapshot metrics, and slot waiting
(`wait=`/`Condition` in the pool) were decided to be kept).

No open questions remain: a string scope instead of a typed `user_id` —
decided (see the scope point above). What may write the config file —
decided: only a sync writes it, and only the lineup it merged; admin verdicts
live in the store disabled-doc, and no sibling-JSON overlay exists.

The public signature `ask(prompt, operation=, trace_id=, wait=)` keeps its
shape — `operation` is optional (decided): scores for calls without a
label accumulate in a separate (model, None) bucket; `trace_id` is unchanged
and `wait` carries the whole-call-deadline meaning described above.
`record_quality` accepts (model, operation, score) and an optional `call_id` —
a pass-through reference for the host UI's journal.

# llmbroker — current architecture

`llmbroker` routes LLM calls over a configured pool of endpoints
(`base_url + model + api_key`). When an endpoint returns 429 or 503, the broker
cools it down and retries the next available one. The caller gets a result or an
exception — never silence.

**Error contract.** When no model is available, the broker raises one
exception (`NoLLMAvailableError`) carrying a machine-readable `reason` and,
when the pool is only temporarily exhausted, the earliest time a model is
expected back (`retry_at`). A client-side request error (any 4xx other than
429/401/403) never cools the model down — it fails over to the next model
within the same call, excluding the failing one for the rest of that call
only; if every candidate rejects the request this way, the last provider
error is re-raised to the caller instead of a generic "no LLM available". It
also outranks a `wait` budget that expires later in the same call: an error the
caller can act on beats "the clock ran out".

**Every failure below the status line fails over too.** A transport failure of
any kind (connect, read, write, protocol, proxy, timeout, or a plain OS socket
error) is treated as a provider-side failure: cool down, journal, next model.
So is an HTTP 200 whose body is not an OpenAI-compatible chat completion —
undecodable JSON, or a shape with no assistant message — which surfaces as
`InvalidProviderResponseError` carrying the model name and a truncated body
snippet; an endpoint answering 200 with garbage is misbehaving no less than one
answering 503. The caller therefore never receives a raw transport or parsing
error from a pool call while another model could still answer. An unexpected
exception is a bug and does reach the caller, and a cancelled call propagates
untouched, but the acquired slot is released on both paths — nothing can
permanently shrink a model's `parallel` capacity.

Malformed means malformed *in the answer*. A reported token count that no
64-bit integer column can hold is discarded and the answer is returned: the
reply is what the caller asked for, so failing the call and cooling the model
over an unusable accounting field would trade a good answer for none. Discarding
it is not cosmetic — a count the journal cannot store loses the whole row, and
with it the call the pool needs to learn from.

**An empty answer is an answer.** A well-shaped completion whose assistant
message carries no text and no tool calls is returned as an empty string, not
raised as a provider failure. Empty output is a legitimate outcome (a filtered
or refused generation), and one prompt that reliably produces it would otherwise
cool down every model in the pool in turn and end in `NoLLMAvailableError` — a
far worse failure than the empty reply itself. A model that answers emptily too
often is the quality score's business, not the failover path's.

**`wait` is the caller's budget for the routing path.** It bounds both halves of
a call: how long the broker may queue for a slot, and how long the model it
picked may take to answer — a provider that accepts the connection and then
hangs cannot outlive the caller's budget. What it does not bound is the broker's
own bookkeeping between attempts: each failed attempt is journaled before the
next one starts, so a call that fails over across several models overruns `wait`
by the store's write latency. That write stays on the call path deliberately —
the journal is the shared state a sibling node reads a cooldown from, and a
caller released before the row lands would let the next one repeat the failure.
`wait=None` (the default) waits as long as at
least one model can still come back by itself (a cooldown expiring, a capped
slot releasing), and raises immediately when none ever will (an empty pool,
every model keyless, every model disabled, or every candidate excluded for
this call); the in-flight attempt then falls back to a single global HTTP
ceiling. `wait=0` is the one asymmetric case: it means "do not queue", not
"answer instantly" — every currently-free model is tried, no cooldown or busy
slot is waited on, and each attempt runs under the global ceiling. A negative
`wait` is legal and means "the budget is already spent": both slot acquisition
and the attempt short-circuit, and the call raises without opening a request. It
needs no validation of its own — `wait=0` is the boundary that carries the
special meaning. There is no per-model timeout knob and will not be one: a
latency budget belongs to the call, not to the model, and a per-model number
could not compose with failover.

**For a stream, `wait` bounds the wait for the first delta.** A stream has no
single "the attempt finished" moment, so the budget covers the one stretch that
is still the broker's to rescue: acquiring a slot and reaching the first delta.
Past it the pace is the consumer's as much as the model's — a caller that
processes deltas slowly suspends the stream between them — so blaming the model
for the wall clock there would be blaming it for its reader. The stream is not
left unbounded: every read after the first delta stays under the global ceiling,
so an endpoint that goes quiet mid-answer still dies rather than hanging.

**A spent budget is never a model's fault.** When the caller's `wait` runs out
while a model is answering, that model is not cooled down and its failure
streak does not advance — the call raises `NoLLMAvailableError(reason="timeout")`
and the journal row carries no `cooldown_until`. Only the global ceiling firing
means the model is genuinely too slow, and that cools it like a 5xx. Without
the distinction a tight `wait` would teach the broker that healthy models are
failing. The row is a plain `ERROR` one: an expiry is journaled for visibility,
not classified, so there is no status of its own to read it back by. Nothing is
cooling either, so the raised error carries no `retry_at` — there is no moment
at which retrying would be better than now.

**But an expiry still teaches ordering.** It is evidence, and the only evidence
obtainable: a model that never answers produces no successful rows, so its
latency cannot be measured any other way. What the expiry proves is a lower
bound — "this one did not answer within X seconds" — and that is enough to stop
handing it to the next caller whose budget is no larger, so that a hung endpoint
costs one caller rather than all of them. The model is not cooled and not
counted as failing; it simply stops being the *first* choice for equally tight
budgets, for a bounded window. A fresh expiry extends that window and may raise
the bound; a window allowed to lapse retires the bound with it, so stale
evidence is never the floor a later, smaller miss builds on; and a successful
answer erases it outright. Three properties keep this from becoming a penalty in
disguise:

- **It is budget-relative.** A caller with a larger budget, or none at all,
  ignores the bound entirely. So the signal can reorder a pool but never
  overturn one: when nobody can meet a budget, every candidate carries a bound,
  the term is equal for all, and curated order stands. It can only ever express
  "this one is slower than its siblings".
- **It never withdraws a model.** Ordering only — a bounded model is still
  selected when it is the last candidate standing, which is exactly when a
  caller would rather have a slow answer than none.
- **It is node-local, by the nature of the thing and not to save work.**
  Latency is a property of the *path* — this node's egress, region, resolver —
  so one node's failure to reach a model in time is weak evidence for another's.
  A cooldown is shared precisely because the thing it describes, a quota, is a
  property of the *key*, which genuinely is shared.
- **It is one signal for both routing paths, deliberately approximate.** A
  stream contributes the budget it missed reaching the first delta, a completion
  the budget it missed answering in full, and neither is scaled before being
  recorded. In one direction that is exact — a model that produced no first
  token within X would not have finished within X either; in the other it
  overstates, since a slow full answer may still start promptly. Ordering is all
  it can affect, so a second signal with its own window and its own reset would
  cost more on the acquisition path than the sharper bound is worth.

An expiry that fired before the attempt reached the provider — the budget was
already spent when the slot was taken — teaches nothing: the model never got a
chance, and recording that would blame it for the caller's clock.

---

## The three pluggable backends

Every host plugs in up to three backends; only the registry is required:

| Backend | Contract | Default (zero-dependency) | What it is |
|---|---|---|---|
| **config** | `RegistryProtocol` | `Registry(path)` (file: `.toml`/`.json`) | where LLM configurations are stored — the merged lineup, see "Syncing the lineup" |
| **secrets** | `SecretsProtocol` | `Secrets()` (env vars, optional `.env` fallback) | how `api_key_ref` names resolve to real keys |
| **store** | `StoreProtocol` | `FileStore(path)` (`store/` dir) | append-only call journal plus the admin disabled-verdict map; see [`optimizer.md`](optimizer.md) |

The store is the only storage llmbroker owns and writes: the append-only call
journal, the admin disabled-verdict map, and any future operational data
(aggregates, per-user settings).

**The default secrets backend reads a `.env` file, without a dependency.** A
broker whose config source is a file (a `.toml`/`.json` path, or a file
`Registry` object) defaults to that file's sibling `.env` as a fallback
consulted only when the real environment has no such variable — the exported
value always wins, and a missing file is simply an empty fallback. The parser is
stdlib-only (`KEY=VALUE` lines, `#` comments, no interpolation) and a malformed
line is skipped rather than fatal. An unfilled `KEY=` line counts as absent, not
as an empty key: the skeleton `llmbroker env` prints is all unfilled lines, and
each must leave its model inactive rather than route to it with no credential.
The file is re-read when it changes, so a key filled in while the broker runs
takes effect on the next resync exactly as an exported one would. This is what
makes the documented quickstart (`llmbroker env … > .env`) work as written; a DB
source or an explicit `secrets=` object is unaffected.

**Where each kind lives:**
- **Contracts** (`RegistryProtocol`, `SecretsProtocol`, `StoreProtocol`, …) live in
  `llmbroker.protocols` — implement one to add a custom backend. They are not part of
  the top-level surface.
- **Zero-dependency implementations** that work without any external backend live in
  `llmbroker.standalone` and are re-exported for convenience: construct them directly as
  `llmbroker.Registry`, `llmbroker.Secrets`, `llmbroker.FileStore` (plus variants
  `DictSecrets`, `InMemoryStore`). This is the simplest usage — a config file,
  env-var secrets, a file-backed store, no integration code.
- **Dependency-carrying backends** are one subpackage per driver (`llmbroker.sqlite`,
  `llmbroker.postgres`, `llmbroker.mongodb`, `llmbroker.aws`, `llmbroker.vault`), each
  re-exporting its own classes from its `__init__.py` (e.g. `llmbroker.sqlite.Registry`)
  — these can't live on the top-level `llmbroker` package the way `standalone` does,
  since that would force the optional driver import on a bare `import llmbroker`.
  Importing the subpackage is the dependency declaration. Internally, each of sqlite/postgres/mongodb is one
  storage `Driver` (`backends/driver.py` — `fetch`/`get`/`upsert`/`delete` for
  registry/disabled/secrets, `append`/`recent`/`purge` for the journal) behind
  one shared port implementation (`backends/ports.py`) written once against the
  `Driver` protocol; adding a new DB backend is one driver file. A custom backend
  outside this package implements either one `Driver` (to reuse the shared ports)
  or a full port protocol directly.

**Source-parameter dispatch.** The broker's first positional argument is the data
source; passing a plain string/`Path` dispatches on its form: `.toml`/`.json` → a
file registry with env-var secrets; a sqlite path/URL (`.db`, `.sqlite`,
`sqlite://…`) → sqlite backing all three ports from one file; `postgresql://…` /
`mongodb://…` → postgres/mongodb backing all three ports from one driver. An
unrecognized form raises a clear error naming the accepted ones; a missing extra
raises an actionable `pip install llmbroker[...]` message. Each backend package is
imported lazily so a bare `import llmbroker` still never pulls in a driver.
Explicit `registry=`/`secrets=`/`store=` arguments always win over whatever the
source would have supplied — passing a already-constructed `RegistryProtocol`
object as the first argument (instead of a string) skips dispatch entirely.
`aws`/`vault` are single-port secrets backends and stay override-only.

---

## What is implemented

### Core

- `AsyncBroker` — async engine. Parallel requests to one LLM are allowed by
  default; `parallel` caps simultaneous in-flight requests per LLM (1 =
  serialize). A cooling LLM is skipped until its cooldown expires. Lazy start
  (no `start()` call required). `aclose()` / `async with` lifecycle. LLMs are
  identified by name; access is always by name.
- `Broker` — synchronous wrapper over `AsyncBroker` on a dedicated background
  event-loop thread. First-class shipped surface, not an afterthought.
- `optimize` parameter shape (`bool | Optimizer`) is locked. `optimize=True` (default)
  activates the `Optimizer` component: provider-trusted cooldown durations and
  per-operation quality demotion. See [optimizer.md](optimizer.md) for the behavior
  spec.
- `ensure_pool()` — lazy idempotent pool initializer with double-checked locking;
  loads the registry into the pool, raising if it is empty (sync a lineup into it
  first). Called automatically by every method that routes or views the live pool
  (`ask`/`chat`, `snapshot`, `get`, `count`, `disable_llm`/`enable_llm`,
  `record_quality`, `__aenter__`) and by no journal read — see "Journal read
  path". Call it explicitly for eager fail-fast startup.
- `sync(source)` — merges a lineup into the registry and returns a `SyncReport`.
  `source` is a curated preset name, a config file path, or a registry. The only
  registry write path; there is no `add`/`update`/`remove`. See "Syncing the
  lineup" below.
- Plain `KeyError` signals a missing LLM everywhere in the public API.
- **Scoping** — an opaque `scope: str | None` string on the broker prefixes secret
  refs (own key, falling back to the shared ref) and attributes journal rows; the
  registry and everything the optimizer learns are always global. See "Per-user
  scoping" below.

### Batteries

| Backend | Implemented |
|---|---|
| Registry | `Registry(path)` (file, `.toml`/`.json`), `llmbroker.sqlite.Registry`, `llmbroker.postgres.Registry`, `llmbroker.mongodb.Registry` |
| Secrets | `Secrets()` (env), `DictSecrets(mapping)` (test double), `llmbroker.sqlite.Secrets`, `llmbroker.postgres.Secrets`, `llmbroker.mongodb.Secrets`, `llmbroker.aws.Secrets`, `llmbroker.vault.Secrets` |
| Store | `FileStore(path)` (day-split journal + YAML disabled map), `InMemoryStore()`, `llmbroker.sqlite.Store`, `llmbroker.postgres.Store`, `llmbroker.mongodb.Store` |

### CLI

- `python -m llmbroker env <config-or-preset>` — emit a `.env` skeleton of
  `api_key_ref` names, in file (`llms` declaration) order, with each one's `help` text
  (see "Key acquisition help" above). The argument is a local config file when one
  exists at that path, otherwise a preset name fetched the same way `preset` fetches
  it — so a first-time user needs no local file at all. Onboarding is folded into
  this command rather than a separate `setup`/`status` command, to keep the CLI
  surface small.
- `python -m llmbroker preset <name>` — print a curated preset TOML to stdout
  (redirect to save: `preset freetier > freetier.toml`), or `--sync FILE` to
  refresh that file in place: the managed pool entries and their keys from the
  preset, and every alias-following custom entry from the paid catalog (see
  "Direct model access and stable aliases"). It prints the `SyncReport` on every
  run, no-ops included, and exits non-zero only on a failure — a pending key or a
  kept entry is a valid state, not an error.
- `python -m llmbroker add-model` — pick a paid provider and model from the
  curated catalog and append it as a custom entry. It follows the catalog's
  alias by default so later refreshes keep it current; `--pin` writes a
  version-pinned entry instead, which no refresh touches.

**The CLI writes files only, and a DB-shaped `--sync` target is refused.**
Mirroring a lineup into a registry is the application's own entrypoint calling
`broker.sync(...)`, built by the same factory the application uses — the alembic
`env.py` split: the library owns the operation, the host owns the connection. A
CLI that took a DSN would duplicate connection config the application already
owns (syncing one database while serving from another is a silent failure) and
would force DB credentials into the CLI's environment, which an application
fetching its DSN from Vault cannot supply.

### DB schema

Every DB backend self-manages its schema via `ensure_schema`: idempotent, checked
once per driver instance before any operation, version-aware. Every
table/collection is `llmbroker_`-prefixed so the host's migration tool can ignore
them by prefix. Single-known-installation policy: `ensure_schema` creates the
current shape fresh when no version marker exists, and raises an actionable
typed schema-version error carrying the found and expected versions on any other
version mismatch — there is no in-place
`ALTER`-based migration path; upgrading means dropping the `llmbroker_*`
tables/collections and restarting (export registry/secrets/calls first if needed).

**The table schema is not a public contract.** A host may query `llmbroker_calls`
or the other tables directly, but at its own risk — column names and shapes may
change between releases without notice. The supported read surface is
`snapshot()` (raw per-model facts + metrics); hosts that need more should read
through a `QueryableStoreProtocol`/`DisabledMapProtocol` backend, not the
raw table.

### Journal read path

The journal has two read forms, both newest-first and both over the same store
port: a tail of raw records, and a per-model aggregate of call records over a
time window. Both narrow by an inclusive lower time bound, by record kind, and by
operation — the kind filter matters because the two record kinds interleave in
one stream and a quality record carries no status, so a host aggregating call
outcomes without it gets a silently wrong denominator; the operation filter
matters because the journal is shared by everything the broker calls, including
broker-internal traffic a host never issued.

The operation filter matches a named operation only: an unset filter means "do
not filter", so calls journaled without an operation label cannot currently be
isolated as a group. A host that labels none of its calls therefore has two
readings — everything, or one named operation — and neither is "mine". This is
sound while the broker journals no traffic of its own; it stops being sound the
moment the broker writes rows under its own operation name, which is the point at
which the filter needs a way to select the unlabelled bucket.

**Journal reads never provision the pool** — a rule binding on every journal-read
API, present and future, not an exception granted to one method. The journal's
rows do not depend on the registry, so a visibility call must keep working on an
install whose registry is empty, stale, or gone — precisely the state a host UI
most needs to render. This separates them from `snapshot()`, which is a view of
the *live pool* and so does provision. Consistency with the routing methods is
the weaker argument: those provision because they route, and a read does not.

Window aggregates are derived per request from the journal, never accumulated
into stored counters — see [`decisions.md`](decisions.md).

**Every instant crossing the store boundary is UTC, in both directions.** A
journal record's timestamps are pinned on write and a caller's time bound is
pinned on read; a naive value is refused at either boundary rather than guessed
at, because guessing shifts it by the writer's or caller's offset on some
backends and not others — silently, and in the one API whose purpose is an exact
window. The rule has to be symmetric: a naive value admitted on write resurfaces
as a mis-filed record or a failed comparison on every read that follows. The row
limit must be at least 1: backends disagree on what zero means (one reads it as
"no limit"), so a caller's shrinking budget must not decay into a full scan.
Both are enforced at the public API as well as in the shipped backends, so the
guarantee does not depend on a host's own store implementation upholding it.

Four tables/collections exist: **registry**, **secrets**, **disabled** (admin
verdicts, seeded with model names at `sync`), and **calls** (the journal). There
is no state or summaries table — shared cooldowns and learned quality derive
entirely from the calls journal (see [`optimizer.md`](optimizer.md)).

- **SQLite** tracks version via a single-row `llmbroker_schema_version` table,
  like the other two backends: the marker lives inside the `llmbroker_*`
  namespace, and the file header (`PRAGMA user_version`) stays the embedding
  application's — see [`decisions.md`](decisions.md). The driver deliberately
  does not manage `journal_mode` (it never enables WAL) or `busy_timeout`:
  journal mode is a persistent, file-level property owned by whoever owns the
  database file, so on a database shared with the host the host owns it and on a
  broker-only file the operator sets it once, out of band — see
  [`decisions.md`](decisions.md).
- **Postgres** tracks version via a single-row `llmbroker_schema_version` table
  (no PRAGMA in Postgres). Passing an existing `asyncpg.Pool` means the caller
  owns its lifecycle and `aclose()` is a no-op; passing a `postgresql://…` source
  string instead makes the driver create and own the pool, closed by `aclose()`.
- **MongoDB** tracks version via a document in `llmbroker_schema_version`. Passing
  an existing Motor database means the caller owns the client; passing a
  `mongodb://…` source string instead makes the driver create and own the client.

#### Columns vs. JSON

A field earns a dedicated column only if it appears (or realistically will) in a
`WHERE`/`JOIN`/`ORDER BY`/`GROUP BY`/aggregate; everything else is payload and
lives in a single JSON column (JSONB on postgres, TEXT on sqlite; a native
sub-document on mongo) keyed by the row's identity. This is a hybrid, not "JSON
everywhere" — identity and queried fields stay first-class columns and keep their
indexes.

Per table:

- **Calls** (`llmbroker_calls`) — the store journal: `id`, `llm_name`,
  `called_at`, `kind` (`call`/`quality`), `scope`, `status` are queried/indexed
  columns; open-ended provider extras live in the `usage_extra` JSON column;
  `cooldown_until`/`key_hash` ride on failed rows for the shared-cooldown rebuild
  (see [`optimizer.md`](optimizer.md)). `called_at` is indexed, so a time-bounded
  read is an indexed scan on every SQL backend.
- **Registry** (`llmbroker_registry`) — hybrid: `name`, `base_url`, `model`,
  `api_key_ref` stay columns (identity, plus stable human-meaningful config);
  nested/open-ended per-LLM config (e.g. `parallel`) lives in the `metadata`
  JSON column. The registry is global (no scope column) and holds the merged
  lineup (see "Syncing the lineup") — nothing but `sync` writes it, and it holds
  no learned data.
- **Disabled** (`llmbroker_disabled`) — the admin disabled-verdict map: a flat
  `name -> disabled` mapping, one row per model name. Written only by
  `set_disabled` or seeded (missing names only, `disabled: false`) by `sync`/
  provisioning.
- **Secrets** (`llmbroker_secrets`) — a flat `ref -> value` store, keyed by `ref`
  alone (no scope column — the broker folds the scope into the ref string as a
  prefix, see "Per-user scoping"); `value` is a single opaque scalar with no
  sub-structure, so JSON buys nothing.

`LLMState` and `LLMConfig` (`src/llmbroker/models.py`) are the typed dataclass
boundary for the JSON payloads.

### Host migration coexistence

`llmbroker.integrations.alembic.include_object` — a predicate for Alembic's `include_object`
hook that excludes every `llmbroker_*` object from autogenerate. Zero Alembic
dependency: the hook inspects the object name only. Wire it in `alembic/env.py`:

```python
context.configure(include_object=llmbroker.integrations.alembic.include_object, ...)
```

---

## Preset distribution

Curated LLM lists live in `presets/` at the repository root — not in the wheel.
A list update is a plain commit, independent of any package version. The
`preset <name>` CLI command fetches from the repository default branch:

```
https://raw.githubusercontent.com/andgineer/llmbroker/main/presets/<name>.toml
```

Presets are curated, multi-provider free-tier pools only: a paid-tier preset
defeats the point (anyone willing to pay uses one good model directly — no
pooling needed), and a single-provider preset defeats the point too (the
pool's resilience comes from spilling onto other providers when one is
rate-limited). Presets are not task-specialized or quality-ranked — the pool
has no quality-aware routing to exploit such a distinction, so a preset lists
one genuinely useful model per provider rather than several ranked ones.

When curation replaces a model with a strictly better sibling from the same
provider, the old entry is removed rather than left alongside the new one:
the two usually share one provider quota, and a still-endorsed old entry
would keep spending that shared quota on worse answers. Downstream, the next
sync sees the lineup still carrying that `api_key_ref` and removes the old entry
with no key lookup at all — its ratings and verdicts are gone with it,
since nothing survives outside the journal/disabled map that `sync` never touches
(see "Syncing the lineup" below). See
[`freetier-providers.md`](freetier-providers.md) for how the curated free-tier
preset specifically is kept current.

---

## Direct model access and stable aliases

Application code must not change when a model version changes. A deployment that
follows the curated recommendation for "opus" keeps calling `direct("opus")`
while the catalog moves that alias from one generation to the next; only the
minority that genuinely needs a fixed version pins one.

**Only user-owned `[[custom]]` entries are reachable directly.** Pointing
`direct` at a preset-managed pool entry raises `PoolModelError`. The pool is
anonymous by design: its members are reached through `ask`/`chat`/`stream`,
which route and learn, and choosing or debugging an individual pool model from
application code contradicts that. A model a host wants to call by name is a
`[[custom]]` entry — that is what the array is for.

**A custom entry carries two identifiers with disjoint roles.**

- *name* — the full identity, following the convention preset entries already
  use: the provider id, then the model id. It carries the version, and the
  registry, journal, learning, and visibility all key on it exactly as they do
  for pool entries. For an alias entry the tooling writes it, because it must
  change when the followed version does.
- *alias* — the eternal handle: what the application passes, and the id of the
  catalog line the entry follows. It never carries a version.

**Alias presence is the followed/pinned switch.** With an alias, the entry's
provider fields are catalog-managed and a refresh rewrites them. Without one,
the entry is entirely the user's and a refresh never touches it — a pin needs no
syntax of its own.

**Learning resets by name change, with no dedicated mechanism.** A refresh
rewrites the model id and the entry name together, so journal rows for the old
name orphan naturally and the new model starts clean. Scores learned for one
version never carry to another.

**The two lookup keyspaces are disjoint, and naming one is a version
assertion.** A call names either an alias or a name, never one string that could
be both — so there is no cross-uniqueness rule to enforce, call sites document
themselves, and asking for an entry *by name* fails loudly once a refresh has
moved the alias on, instead of silently running a newer model. A miss whose
string exists in the other keyspace says so.

**Aliases are unique across the whole catalog** and permanent: a published alias
never disappears and never renames — a generation change re-points it at the
successor model. A duplicate makes the catalog invalid and is refused with an
error, like any other bad catalog.

**A name identifies exactly one entry, across both arrays.** Every store keys on
it — a DB registry's primary key, the live pool's slot map — so a config
carrying a name twice does not raise an ambiguity to resolve later, it loses an
entry at the next sync. An alias entry's name is machine-formed in the same
`<provider>-<model>` convention preset pool entries use, so a catalog move can
land one on the other; that is refused where it would be introduced (loading a
config, and `--sync` before it writes anything) rather than tolerated. When a
name does resolve, the user's own entry is what it means: `direct(name=…)`
searches custom entries, and a pool entry of that name only decides which error
comes back.

**Refreshing is a file rewrite, never a runtime lookup.** `preset <name> --sync
FILE` re-points every alias entry in the file at what the catalog now
recommends, printing one line per change; an alias the catalog no longer knows
is a warning and its entry is left untouched. The file stays the single source
of truth and `sync` stays offline — nothing consults the catalog at runtime or
at sync time.

## Pool streaming

The routed pool streams as well as it answers: deltas arrive as the provider
produces them, over the same routing, failover, and journaling as a pooled call.
It is async-only, like the direct client's streaming.

**Failover ends at the first delta.** Every failure before it — a 429, a 5xx, a
transport error, a malformed response — cools the model down and moves to the
next candidate exactly as a non-streaming attempt would, through the same
classification; there is not a second failure surface for streams. After the
first delta the answer is already partly the caller's, and no design can rescue
it: retrying elsewhere would either duplicate the text already delivered or
splice two models' prose together. So a mid-stream death cools the model (it
misbehaved no less than one failing earlier) and raises, carrying the model name
and the underlying cause; the deltas already yielded stand.

Each attempt journals one row, as `chat` does. **A consumer that stops pulling
ends a successful attempt**, not a failed one: the model answered and did
nothing wrong, so the row is `OK` — abandoning an iterator must never cost a
model a *failure*.

**The slot goes back when the iterator is closed, and closing it is the
consumer's move.** An async generator has no other signal: the broker cannot
tell "paused between deltas" from "never coming back", and the provider
connection is still open either way, so holding the slot until close is correct
rather than conservative. Python closes the iterator for the ordinary shapes —
`break`, an exception through the loop, a cancelled task — because the last
reference drops there. A consumer that keeps the iterator in a variable and
walks away holds the slot until the event loop finalizes it, so a long-lived
host that abandons streams that way must close them itself
(`contextlib.aclosing`). This is the standard async-generator ownership
contract, not a broker rule.

---

## Key acquisition help

A config source may carry, per `api_key_ref`, a short markdown `help` string (a
link plus a step or two) plus a free-form `extra: dict[str, str]` passthrough of
whatever else the TOML `[keys.REF]` section holds — llmbroker has no taxonomy
opinion on it, it just relays whatever the preset author put there. It is keyed
by the env-var name, not by LLM, because one key is typically shared by several
LLMs.

The same data feeds two consumers:

- the `env` CLI prints keys in file (`llms` declaration) order, each with its
  `help` line above its variable;
- a host can pull `extra` to render its own setup UI (e.g. its own effort/value
  taxonomy, a daily-cap note).

Surfacing it is an **optional registry capability** (`key_info() -> dict[str,
KeyInfo]`), independent of the broker. A registry that has the metadata exposes
it; one that does not simply omits the capability. Hosts query whichever registry
they hold — no coupling between obtaining the help and routing.

An unresolved `api_key_ref` is normal, not an error: the pool routes over whatever
keys are present, and a config without a resolvable key simply stays inactive
(logged at `info`, not `warning`) rather than enqueued for routing. The only
genuine alarm is **zero** keyed configs at all — see [`optimizer.md`](optimizer.md)
for how that's detected and raised.

**A key exists only when it is non-empty.** An env var exported blank, an unfilled
`KEY=` line, a backend returning `""` — all count as unset everywhere, since key
presence now also authorizes removals during a sync (see "Syncing the lineup").

There is no background key re-resolve loop: a key added to the environment
after startup takes effect at the next `ensure_pool()` call (fresh process, or
an explicit re-provision) or immediately if the host calls `sync` again
(it re-bootstraps any newly resolvable secrets) — never via a polling task.

---

## Syncing the lineup

`sync(source)` is the only registry write path — there is no `add`/`update`/
`remove` — and it returns a `SyncReport` describing what it did.

**One verb, two sources.** A *preset name* (`sync("freetier")`) fetches the
curated lineup from the catalog: this is the only networked operation in the
library. A *path* or *registry* (`sync("llms.toml")`) is offline. Both then run
the identical merge, so the rules below hold whatever named the lineup.

**A lineup that stops updating decays into an unusable pool.** The pool is a list
of free endpoints that providers retire without notice, so a broker following the
curated preset keeps it current on an interval — checking lazily on activity, never
on a timer — rather than waiting for an admin to run something. That is what makes
the library work at all, so there is no off switch: an installation that must not
follow our curation declares a lineup of its own instead, which is a different
pool rather than a frozen copy of ours.

Concurrent nodes are safe because the merge is a pure function of (arriving
lineup, current lineup, resolved keys), and one registry means one secrets store,
so every node computes the same result: the first write settles it and the
identity gate turns every other node's check into a no-op. What a node must never
do is coerce the shared registry to a *local copy* of its own — diverging copies
would flip-flop it. An implicit refresh follows the one shared upstream, which is
why nodes converge instead of oscillating.

### Two tiers, one merge site each

|  | tier 1 (the common case) | tier 2 |
|---|---|---|
| registry / secrets | `llms.toml` on its default env/`.env` secrets | DB / Vault / AWS, possibly per-user (`scope`) |
| who merges | the CLI, into the file | `broker.sync(...)` |
| key visibility | `os.environ` + the file's sibling `.env` | the broker's own secrets backend |

Each tier has exactly one merge site, and that site sees the same keys the
application will. In tier 1 the CLI resolves through the same env-plus-sibling
`.env` pair a file-configured broker uses, so what the CLI decides is what the
application would have decided. **A file registry paired with a Vault/AWS/DB
secrets backend is tier 2, not tier 1**: only the broker can see those keys, so
that installation refreshes from code even though its lineup lives in a file.
Tier 2 never merges from the CLI at all. That is what makes a key-aware merge
safe: no merge ever runs blind to the keys the program that consumes its output
will have.

A file target is written from a curated preset only. A file or registry source
syncs into a database registry — the vendored-lockfile deploy path, where the
merge dedupes `[[custom]]` entries; rendering an arbitrary source into a live
`.toml` cannot.

### The removal rule: the provider is the unit

Only managed entries whose name is absent from the arriving lineup are candidates
for removal. An entry still present is updated in place, and `[[custom]]` entries
are never pruned.

**The unit of decision is the `api_key_ref`, not the entry.** Two entries on one
ref are one quota and one failure domain; counting them as two of anything is
wrong. For each dropped entry, in order:

1. The arriving lineup still carries that `api_key_ref` — its models replace the
   entry: same key, same quota, removed with no key lookup at all.
2. Otherwise, if keys are visible here (below) and no key exists for that ref, the
   entry is removed: nothing could call it anyway.
3. Otherwise, if this installation's own journal proves the entry does not work,
   it is removed and reported as **retired**.
4. Otherwise it stays — "kept" — and keeps routing.

Hence the invariant that is the whole safety story:

> **A sync never takes away a model this installation can call**, except by
> replacing it with the same provider's model, or when the journal says the model
> does not work.

The rule depends only on the state of the world — which providers the lineup
carries, which keys exist, what the journal recorded — which is what makes
repeated syncs converge instead of oscillating.

A path source is therefore not a blind mirror: an operator who deletes an entry
from the vendored file gets it removed only under the rule above, and the report
says why it was kept. The escape hatch for a forced lineup is
`registry.mirror(configs)` directly.

**Death is proven, never assumed.** An entry is dead when this installation's
journal window holds at least one permanent client failure (401/403/404) and no
successful call at all. A bad week — 429s, 5xx — proves nothing. The journal is
read only when there is a candidate to decide about, which is never on an
ordinary sync; a busy pool that pushes the failure out of the bounded tail leaves
no evidence, and the entry stays. Conservative on purpose.

A candidate is any entry the rule above would otherwise keep. Where a missing key
*is* evidence that covers entries whose key is here; where it is not — per-user
keys, a probe that resolved nothing — it covers every dropped entry, because
"nobody could call it and lived" is then the only evidence that installation can
produce, and it is strictly stronger than key absence. Without that, a per-user
host could never retire anything.

**A retirement shows its evidence.** Deleting an entry from the installation's
own configuration is the one destructive thing a sync does, so the report carries
the permanent status the provider answers now and how far back the run of
failures reaches in the window that was read — the code an admin is sent to check
has to be the one they will see. An admin can check the verdict without opening
the journal.

**A key exists or it does not.** A key exists when the secrets store returns a
**non-empty** value for its ref (an empty or whitespace-only value is no key,
whatever backend produced it), or when the ref is declared in `have_keys`.

**Absence of a key is evidence only where the probe could have found one.** Two
merge sites cannot prove absence, and at both of them a dropped provider's entry
is kept regardless: an installation whose keys are per user behind `scope`, and
one whose probe resolved *nothing at all* — there the keys live in a store this
merge site cannot reach, and "no key anywhere" is indistinguishable from "not the
keys this lineup runs on". `have_keys` is how an installation that knows better
overrides either.

**`have_keys` only lowers conservatism.** `have_keys=["OPENAI_API_KEY"]` (or
`True`) declares refs the broker cannot probe — per-user keys behind `scope`, a
secret injected only in production. Declared refs count as present when the merge
weighs a removal, and only there: `have_keys` never makes a model routable, the
pool still needs a real key value. It is a promise — declare a ref and fail to
provision it, and the pool degrades (old entries removed, replacements inactive).
Omit it and nothing breaks; the lineup just keeps entries a better-informed run
would have pruned.

**A sync never deletes a secret.** A key that cannot be deleted — one kept for
paid direct calls on the same provider — is the common case, so deletion could
never be the retirement mechanism. When a removal leaves a ref whose key *is*
here and that nothing in the merged lineup references any more, `[[custom]]`
included, the report says the key is now unused and a human decides; `[keys.REF]`
is kept while any entry still references it. A ref with no key behind it is
nothing to revoke, and saying otherwise would put an invented admin act into the
one channel that exists to surface the real ones — on the commonest removal of
all, a curated lineup dropping a provider this installation never had a key for.

**Retention is recomputed, never stored.** Which entries are kept follows from
(arriving lineup, current lineup, keys) on every merge, so a persisted flag would
be an output masquerading as an input. Nothing in `LLMConfig` or the TOML records
it; the file writer groups kept entries under a generated comment, and the report
names them on every run, including no-ops.

**Model identity is immutable.** The same name with a different `model` is a
synchronous error — a model bump must be a new entry name. This protects the
binding between a model's learned quality stats and its name.

**One structural guard.** Applying a result with zero entries over a registry that
has some is refused with `SyncRefusedError`, carrying the report; an empty
registry accepts anything, which is onboarding. The rule above can reach it — an
empty lineup over a registry whose entries are all keyless removes everything —
so the guard is on the normal path, not a backstop.

Nothing is lost by a removal that does happen: keys live in the secrets store,
learned state derives from the journal, and admin verdicts live in the store
disabled map, so a model returning later is re-added and its old ratings and
verdict resurface.

### Visibility: raw facts, admin-facing

- **`SyncReport`** is returned by every sync and printed by the CLI on every run
  *including no-ops*, so kept entries and missing keys nag in each deploy log
  until resolved. `last_sync_report` lets a host forward it to its own admin
  channel. The report carries no severity enum — the host derives criticality.
- **The committed config file is the durable state**: kept entries and the keys
  they still need sit in the file, so a bot refresh is reviewable in the pull
  request diff itself, and the file's git history is the update record. The sync
  stores nothing of its own.
- **A sync that changes nothing is indistinguishable from no sync at all.** The
  merged result is compared with what is already stored, and when they are equal
  nothing is written, nothing is applied to the live pool, and the outcome is
  logged at `debug`. The comparison is on the bytes that would be written for a
  file target, and on the entries keyed by name for a registry target — a
  database hands its rows back in its own order, so position there is the
  backend's and not the lineup's. What the gate covers is the lineup: secret
  bootstrapping runs on every sync, since a key that has just appeared in the
  environment is the reason a host calls one.
- A sync that *did* change something logs its outcome once at `info`, whatever
  called it, pending keys and kept entries included. Nothing in a sync outcome is
  admin-actionable: a kept entry is a working model that keeps routing, and a
  keyless entry is a normal documented state. What *is* actionable is a degraded
  pool, and the catalog owns that alarm. The log line and `last_sync_report` are
  both recorded before the pool is reconciled, so a reconcile that raises cannot
  swallow the record of a change already applied. `last_sync_report` is set on
  every outcome, no-ops included, and the CLI prints the report on every run —
  the reviewable path always shows its work.
- The **journal** stays what it is — a stream of LLM calls and quality ratings. A
  sync is a registry operation and never writes there; it only *reads* the
  bounded tail, and only when a provider it might retire has a candidate entry.

### Pool health

**The measure is the provider, not the entry.** Of the distinct `api_key_ref`s
among pooled entries, how many have a key: `providers_usable` of
`providers_total`. Two entries on one ref are one quota and one failure domain,
so they count once.

**One usable provider is degraded**: a single quota with nothing to fail over to,
which is the failover feature's own definition rather than a tuning knob. Zero is
a dead pool. Missing keys are never an alarm on their own — two providers may be
all a host wants. A registry that pools nothing is not a degraded pool but the
absence of one: a host whose entries are all `[[custom]]` or unpooled asked for no
failover and is told nothing about it.

The alarm lives where membership is reconciled, so it covers provisioning, every
resync and every sync in one place: `ERROR` on the transition into one usable
provider ("no failover left") and into zero ("cannot serve any request"), naming
the refs that are missing; one `INFO` on the way back. Transitions of *state*, not
of severity — both of those are errors, and losing the step between them would
mute the moment the pool stops answering at all. Every count that is not degraded
is one state, so a healthy log carries none of these lines, gaining a further
provider is not news, and a broken pool carries exactly one line per change.

**The measure is key presence, and it never lags behind the keys.** A ref that
stops resolving withdraws its slot on the next reconcile rather than keeping the
value it had, so a revoked or rotated key leaves the count at once instead of
after a run of requests that can only fail. The counts and `snapshot()`'s
per-model `has_key` therefore always agree. An administratively disabled entry
still counts its provider: the alarm reports the keys an installation holds, not
verdicts the host set itself and already reads per model.

`snapshot()` carries the same measurement — the per-LLM mapping it has always
returned, plus the counts, the missing keys with their help text, and the same
`degraded` predicate the ERROR uses. One definition, two consumers: an admin UI
needs one call, and the log and the UI cannot diverge. The help text is read from
the registry only when a key is actually missing, so a fully-keyed pool — the
common case — adds no registry I/O to a reconcile at all, and `snapshot()` never
performs any; a registry without key metadata yields empty help but correct refs
and names.

### Keeping the lineup current

`sync=` names the lineup an installation follows — the curated preset unless it
says otherwise, `None` for a registry filled by other means. Two independent gates
decide what a check costs:

- the **time gate** decides whether to go to the network at all. It is a monotonic
  comparison at the top of `ensure_pool()`, the funnel every public operation
  already passes through, so an idle process performs no I/O and schedules no
  wakeups: the library still needs no running service of its own. A background
  timer was rejected for that reason — a process making no calls has no lineup to
  keep fresh, and a sleeping coroutine would have to be owned, cancelled and
  tested against every embedding.
- the **identity gate** decides whether what arrived changes anything (see "A sync
  that changes nothing" above). It also removes the need for a conditional GET,
  which would save a kilobyte and no round trip while proving strictly less.

**A check that just happened is remembered across process exits**, per
(lineup, target), so a short-lived process does not pay a round trip per
invocation and a rolling deploy does not fetch once per pod. The record only ever
makes checks less frequent: it is not authoritative, a timestamp in the future
counts as absent, and losing it costs one extra fetch. It is deliberately not
shared across a cluster — with a daily interval, N nodes cost N small GETs a day,
which is unmeasurable against the fleet's own LLM traffic, and the identity gate
already makes concurrent application a no-op.

**The refresh is off the critical path, with one exception.** An empty registry is
filled before provisioning, blocking, because provisioning an empty registry
raises and there is no alternative; a registry that already holds a lineup is
provisioned from it and refreshed afterwards, so the first call of a fresh process
does not wait on the network.

It is **best-effort and never raises**: a fetch failure or a refusal logs a
warning naming which check it was, stashes the report on `last_sync_report` where
there is one, and continues on the existing configuration. Neither a start nor a
request ever fails over a lineup refresh. The explicit `sync()` call raises
instead — that caller chose to sync and has a plan. The start attempt is guarded
by its own flag, so a provision that failed for another reason and is retried does
not re-fetch.

**A fetched lineup is cached, and the cache is a fallback rather than a source**:
a successful fetch overwrites it, a failed one — offline, or throttled by the
CDN's per-IP limit — falls back to it. Unlike the check record, the cache is
correctly machine-global: what the catalog says today does not depend on which
project is asking.

Because the refresh is unconditional, the catalog's default branch is live
configuration for every installation. A preset carries no code and entry names are
immutable, and the removal rule bounds what a merge can take away — but a
`base_url` decides where an installation's API keys are sent, so a config built
from a *fetched preset* must carry `https://` ones. Pinning the fetch to the
installed version's tag would close the exposure further and is rejected: a preset
fix would then reach nobody until a release of llmbroker, which is the problem the
refresh exists to remove.

### The home directory

Everything llmbroker caches or remembers on its own — the fetched preset text, the
paid catalog, the check records — lives in one directory: machine-scoped by
default, `home=` per broker, `$LLMBROKER_HOME` per machine, falling back to the
platform cache directory and then to a per-user temp directory. Resolution never
raises: each candidate that cannot be written falls through to the next, and
nowhere writable is a supported outcome — the state then lives in process memory
for that run. Nothing kept there is authoritative, which is what makes that
degradation acceptable: a preset can be re-fetched, and a lost check record only
costs one extra check.

### What sync also does

Beyond writing the lineup, `sync` bootstraps secrets: for each config whose
`api_key_ref` cannot be resolved by the configured `secrets=` backend, it tries
`llmbroker.Secrets()` (env vars) and, if found, persists the value via
`secrets.set()`. Existing secrets are never overwritten — admin-edited values
win. It also seeds the store disabled map with any missing model names
(`disabled: false`), never touching existing verdict values.

A file registry is a legitimate target for a curated preset: the merged lineup is
written back to the `.toml`, preserving its comments and `[[custom]]` entries,
which is what lets a file-configured broker keep itself current. Provisioning
against an empty registry still fails fast, naming the sync call that would fill
it.

The write itself is atomic and preserves the target's permissions, and what is
about to replace a live config is parsed and checked against the merge result
first — this is the one code path that can destroy a user's configuration.

---

## Per-user scoping

A multi-user host can give each end user its own LLM API key over one shared
registry and store. `scope: str | None` on the broker (`""` is
rejected — use `None` for unscoped) is the one knob:

- **The registry and everything the optimizer learns are always global** — one
  model list, one set of quality windows and cooldowns, shared by every scope.
  There is no per-tenant registry partition. Storage and the protocols
  (`RegistryProtocol`, `SecretsProtocol`, `StoreProtocol`) have no user concept
  at all — `scope` is an opaque string the broker itself interprets, never a
  parameter any backend or protocol method accepts.
- **Secrets are the one thing that is actually per-scope.** Key resolution
  tries `resolve(f"{scope}/{api_key_ref}")` first, falling back to
  `resolve(api_key_ref)` on `KeyError` — an own key if one is set, the shared
  key otherwise. The fallback policy lives entirely in the broker; secrets
  backends stay plain exact-lookup key-value stores and never see the scope
  string itself, only the already-prefixed ref.
- **The journal carries `scope` as a plain attribution field** (`Call.scope`),
  filterable via `calls(scope=...)`, but it does not partition learning — the
  rebuild's tail read is unscoped by design. 429 cooldowns and dead-key drops
  follow the key hash (a dead *own* key drops the model only for its scope,
  which the key-hash match in [`optimizer.md`](optimizer.md) already handles
  without any registry-level partition); 5xx cooldowns are global (a
  provider-side outage cools the model for every scope, since it has nothing to
  do with which key was used).
- **A broker instance is one scope's view.** The broker never multiplexes
  scopes internally — resolved keys and the per-LLM slot table are per-instance.
  `scope=None` (the default) is exactly the single-tenant behavior.
- **A sync here probes almost nothing, by design.** With per-user keys there is
  no shared value to resolve, so the key probe finds nothing — which is exactly
  why absence authorizes nothing: the sync keeps every entry it cannot prove is
  replaceable. `have_keys` is how an installation that knows better says so, and
  it is the only reason that parameter exists.

---

## Secret naming conventions

Each managed-secret backend uses a deterministic, namespaced path so secrets written by
llmbroker are identifiable and isolated from the rest of the account. Neither backend
has a user/scope parameter — `ref` is the whole identity, already carrying any scope
prefix the broker added (see "Per-user scoping" above).

### AWS Secrets Manager (`llmbroker.aws.Secrets`)

Secret name in Secrets Manager: `{prefix}{ref}` — `prefix` defaults to `"llmbroker/"`.
Secrets created via `set()` carry the tag `{"Key": "llmbroker", "Value": "1"}` for
independent enumeration and cleanup.

### HashiCorp Vault (`llmbroker.vault.Secrets`)

KV v2 engine. KV path: `llmbroker/{ref}`.

---

## Not yet implemented

| Feature | Phase |
|---|---|
| LLM-as-judge quality scoring | P5 |

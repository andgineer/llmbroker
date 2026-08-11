# Backends and wiring

The three pluggable ports, how one source parameter becomes them, the broker's
lifecycle, the DB schema policy, and the journal all of it writes to. The
cross-cutting rules this file elaborates are in
[`../invariants.md`](../invariants.md).

## The three ports

Every host plugs in up to three backends; only the registry is required.

| Port | Contract | Default (zero-dependency) | What it is |
|---|---|---|---|
| **config** | `RegistryProtocol` | the TOML model list in llmbroker's own directory | where LLM configurations are stored — the merged list |
| **secrets** | `SecretsProtocol` | env vars, with an optional `.env` fallback | how `api_key_ref` names resolve to real keys |
| **store** | `StoreProtocol` | a `store/` directory | the append-only call journal plus the admin disabled-verdict map |

The store is the only storage llmbroker owns and writes.

**Where each kind lives** — the map an implementation follows. Contracts sit in
their own module, apart from the top-level surface: implement one to add a
custom backend. Zero-dependency implementations sit together and are the only
ones the top-level package exposes. Every dependency-carrying backend is its own
subpackage, one per driver, so importing the subpackage *is* the dependency
declaration and a bare `import llmbroker` never pulls a driver in.

Internally each SQL/document backend is one storage driver behind one shared
port implementation, written once against the driver protocol — so **adding a DB
backend is one new driver file and no edit to the ports**. A custom backend
outside the package implements either one driver, to reuse the shared ports, or
a full port protocol directly.

**The default secrets backend reads a `.env` file, without a dependency.** The
zero-config installation takes the working directory's `.env` as a fallback
consulted only when the real environment has no such variable — the exported
value always wins, and a missing file is simply an empty fallback. The
parser is stdlib-only (`KEY=VALUE` lines, `#` comments, no interpolation) and a
malformed line is skipped rather than fatal. An unfilled `KEY=` line counts as
absent (invariant 21): the skeleton the `env` command prints is all unfilled
lines, and each must leave its model inactive rather than route with no
credential. The file is re-read when it changes, so a key filled in while the
broker runs takes effect on the next resync. A DB source or an explicit secrets
object is unaffected.

## Choosing a source

**No source is a source — the default installation.** A bare `Broker()` runs the
curated free pool: the merged list in the home directory (seeded on first use
from the fetched, cached or bundled preset), keys from the environment with the
working directory's `.env` behind them, the journal in the home directory too.
That list is what a sync rewrites, and llmbroker is its only author. Where
nothing is writable, the list and the journal live in process
memory for that run — the broker still routes, it just remembers nothing between
runs.

The journal is machine-global here because keys in this mode come from the
environment, so the quota it tracks really is one pool; a journal scattered per
working directory would make every run rediscover the same 429. Two projects on
genuinely different keys are already separated, since a 429 withdraws the key's
capacity rather than the endpoint's, and a project wanting full isolation passes
its own home.

**Source-parameter dispatch.** The broker's first positional argument is the
data source; passing a plain string or path dispatches on its form, and every
form is a database: a sqlite path or URL gives sqlite backing all three ports
from one file; a postgres or mongodb URL gives that driver backing all three
ports. A file path is not among them — a model list is not a path a host names
([`decisions.md`](../decisions.md#the-model-list-is-not-a-path-a-host-names)) —
so an unrecognized form raises a clear error naming the DSN forms, the bare
broker, and passing a registry object; a missing extra raises an actionable
install message. Each backend package is imported lazily so a bare `import llmbroker`
never pulls in a driver. Explicit port arguments always win over whatever the
source would have supplied — passing an already-constructed protocol object as
the first argument skips dispatch entirely. The single-port secrets backends
stay override-only.

**A string moves the storage; an object moves the ownership.** A connection
string says only where llmbroker keeps its own installation, so it keeps
following the curated preset by default. A registry object is content the host
owns, and there what the installation follows must be stated
([`model-list.md`](model-list.md)). Neither form invites a write into the
tables: a connection string says where llmbroker keeps its own state, not that
the state is the host's to edit, and content the host owns arrives through the
port ([`model-list.md`](model-list.md#the-partition-a-sync-touches-only-what-a-sync-wrote)).

**The home directory.** Everything llmbroker caches or remembers on its own —
the fetched preset text, the paid catalog, the refresh-check records — lives in
one directory: machine-scoped by default, overridable per broker and per
machine, falling back to the platform cache directory and then to a per-user
temp directory. Resolution never raises: each candidate that cannot be written
falls through to the next, and nowhere writable is a supported outcome. Nothing
kept there is authoritative, which is what makes that degradation acceptable.

## Lifecycle

`AsyncBroker` is the async engine; `Broker` is a synchronous wrapper over it on
a dedicated background event-loop thread, a first-class shipped surface rather
than an afterthought. Both start lazily — no explicit start call — and support
`aclose()` / context-manager lifecycle. LLMs are identified by name, and access
is always by name; a plain `KeyError` signals a missing LLM everywhere in the
public API.

Pool provisioning is a lazy idempotent initializer with double-checked locking:
it loads the registry into the pool and raises if it is empty, naming the sync
that would fill it. Every method that routes or views the live pool calls it
automatically; no journal read does (invariant 6). Call it explicitly for eager
fail-fast startup.

## DB schema

Every DB backend self-manages its schema: idempotent, checked once per driver
instance before any operation, version-aware. Every table and collection is
`llmbroker_`-prefixed so the host's migration tool can ignore them by prefix.

There is no in-place migration path
([`decisions.md`](../decisions.md#no-schema-migrations)). The schema is created
fresh when no version marker exists, and any other version mismatch raises an
actionable typed error carrying the found and expected versions; upgrading means
dropping the `llmbroker_*` objects and restarting, exporting first if needed.
That is what makes invariant 14 load-bearing: dropping the namespace has to be a
full reset, so the marker lives inside it on every backend.

Passing an already-constructed pool or database object means the caller owns its
lifecycle and closing the broker is a no-op for it; passing a connection string
instead makes the driver create and own the connection.

**Columns vs. JSON.** A field earns a dedicated column only if it appears, or
realistically will, in a `WHERE`/`JOIN`/`ORDER BY`/`GROUP BY`/aggregate;
everything else is payload and lives in a single JSON column keyed by the row's
identity. This is a hybrid, not "JSON everywhere" — identity and queried fields
stay first-class columns and keep their indexes. The journal's time column is
indexed, so a time-bounded read is an indexed scan on every SQL backend.

Four tables or collections exist and no more:

- **calls** — the journal. Failed rows also carry the cooldown instant and key
  digest the shared-cooldown rebuild reads (see
  [`selection.md`](selection.md)).
- **registry** — the merged list. Global, no scope dimension, no learned data,
  and nothing but `sync` writes it. It stores no ordering (invariant 3), so a
  backend author must not assume the load order means anything.
- **disabled** — the admin verdict map, a flat name-to-boolean mapping. Written
  only by the disable verb or seeded with missing names by a sync or by
  provisioning.
- **secrets** — a flat ref-to-value store keyed by ref alone. No scope column:
  the broker folds the scope into the ref string as a prefix, and the value is a
  single opaque scalar, so JSON buys nothing.

There is no state or summaries table: learned quality derives entirely from the
calls journal, and availability is never stored at all (invariant 11).

**Host migration coexistence.** The package ships a predicate for Alembic's
object-inclusion hook that excludes every `llmbroker_*` object from a host's
autogenerate, carrying zero Alembic dependency: it inspects the object name
only.

## Secret naming

Each managed-secret backend derives its path deterministically from the ref
under an llmbroker-owned namespace, so secrets written by llmbroker are
identifiable and separable from the rest of an account, and what llmbroker
created can be enumerated and cleaned up without guessing. Neither backend has a
user or scope parameter — the ref is the whole identity, already carrying any
scope prefix the broker added.

**A ref occupies exactly one path segment.** A scoped ref carries a separator the
broker put there, and a store whose namespace is hierarchical would read it as a
folder and answer a listing with directory names instead of refs. Such a backend
flattens on write and unflattens on read, and refuses a ref that already contains
the flattening marker rather than storing something it could not hand back.

**Listing refs is optional, and worth implementing.** A backend that can answer
"which refs do you hold under this prefix" is asked once per rebuild, and a ref it
does not name then costs no read at all, however many callers want it; one that
cannot is asked ref by ref, which is what an environment-backed store wants —
the lookup is free there and there is nothing to enumerate. Nothing a listing or a
read raises reaches a caller: it is logged and the ref is read as unset, because a
key nobody can fetch must not turn into a failed call
([`model-list.md`](model-list.md)).

## The journal

The only state llmbroker keeps beyond the static registry: how it is read, what
is re-derived from it, and how `scope` attributes rows. Where it is stored is the
store port above.

### The read path

The journal has two read forms, both newest-first and both over the same store
port: a tail of raw records, and a per-model aggregate of call records over a
time window. Both narrow by an inclusive lower time bound, by record kind, and
by operation.

The kind filter matters because the two record kinds interleave in one stream
and a quality record carries no status, so a host aggregating call outcomes
without it gets a silently wrong denominator. The operation filter matters
because the journal is shared by everything the broker calls.

The operation filter matches a named operation only: an unset filter means "do
not filter", so calls journaled without an operation label cannot currently be
isolated as a group. A host that labels none of its calls therefore has two
readings — everything, or one named operation — and neither is "mine". This is
sound while the broker journals no traffic of its own; it stops being sound the
moment the broker writes rows under its own operation name, which is the point
at which the filter needs a way to select the unlabelled bucket.

Every instant crossing the boundary is UTC in both directions (invariant 9), and
the row limit must be at least 1: backends disagree on what zero means — one
reads it as "no limit" — so a caller's shrinking budget must not decay into a
full scan. Both are enforced at the public API as well as in the shipped
backends, so the guarantee does not depend on a host's own store implementation
upholding it.

Window aggregates are derived per request, never accumulated into stored
counters ([`decisions.md`](../decisions.md#aggregates-derived-not-accumulated)).
The library returns per-status counts and leaves failure policy to the host: the
aggregate carries only statuses actually observed, so "how many were not OK" is
a subtraction rather than an assumption about the status enum's shape.

### One tail read, and quality is what it derives

A read of the most recent records re-derives what the host has taught this
installation: quality-window verdicts, the latency bound an expired budget left,
and the snapshot metrics. It runs when the pool is rebuilt and at no other time
([`model-list.md`](model-list.md#keeping-the-list-current)), which is also when the registry and the
admin disabled-verdict map are re-read, so another process's edits reach a
running broker without a restart.

A call record carries evidence rather than a summary of it: one that ran out the
caller's budget carries the budget it missed (see
[`selection.md`](selection.md)). Nothing derived is recovered by reading back a
message the library formatted.

The tail is shared across all models and operations, so a chatty model can crowd
a quiet model's ratings out of it. This is an accepted consequence, and the tail
limit is the tuning knob.

Persistence is the store by default; an explicit in-memory opt-out degrades to
session-scoped learning. That degradation is what the forward fold of invariant 8
carries: a store with no read path never contributes a tail, so a rating and a
missed budget reach the live state only as the row is written, and nothing
survives the process. The journal forgets via retention — every backend
self-purges records older than its retention horizon — and there is no public
purge operation.

The admin disabled-verdict map is the one **excluding** verdict, orthogonal to
quality demotion: values are written only by the disable verb, and llmbroker
only seeds missing names. It survives a sync by construction, since a
sync only touches the registry, and it works identically for file and DB
sources. Lifting a verdict simply lifts it; rehabilitation happens through new
ratings displacing old ones in the window.

### Per-user scoping

A multi-user host can give each end user its own LLM API key over one shared
registry and store. `scope` is the one knob — an opaque string, with the empty
string rejected in favour of the unscoped default
([`decisions.md`](../decisions.md#scope-is-an-opaque-string)).

**A scope is a caller, not a broker.** One broker is the installation and holds
everything installation-global; a caller is what a request holds, and asking the
broker for one costs no I/O
([`decisions.md`](../decisions.md#the-broker-is-the-installation-a-caller-is-a-scope)).
The broker's own call verbs are its unscoped caller, so a host with one tenant
never meets the second noun.

- **The registry and everything learned are user-agnostic** (invariant 16).
  There is no per-tenant registry partition, and storage and the protocols have
  no user concept at all: `scope` is interpreted by the broker, never passed to
  a backend or protocol method.
- **Secrets are the one thing that is actually per-scope.** A caller resolves the
  scope-prefixed ref first and falls back to the shared one — an own key if one is
  set, the installation's otherwise. The shared value is read once for everybody.
  The fallback policy lives entirely in the broker; secrets backends stay plain
  exact-lookup key-value stores and never see the scope string itself, only the
  already-prefixed ref.
- **The journal carries the scope as a plain attribution field**, written by the
  caller that made the call and filterable on read at the store, but it does not
  partition learning — the tail read is unscoped by design, and so is the broker's
  own journal read, which is the installation's view.
- **A rejected key is withdrawn from whoever spent it.** The withdrawal is the
  caller's, not the pool's: a caller with a key of its own is untouched by a
  neighbour's 401, and a caller paying with the shared value loses it alongside
  every other caller paying with the same value, because that is one credential.
  It lasts until the pool is rebuilt, which re-reads the ref and takes it back if
  a working value has appeared. Nothing about it is written or read anywhere
  (invariant 11), so nothing needs a partition.

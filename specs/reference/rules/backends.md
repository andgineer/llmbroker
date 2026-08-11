# Backends and wiring

The three pluggable ports, how one source parameter becomes them, the broker's
lifecycle, and the DB schema policy. What the journal holds and how it is read
is [`journal.md`](journal.md). The cross-cutting rules this file elaborates are
in [`../invariants.md`](../invariants.md).

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
([`list-refresh.md`](list-refresh.md)). Neither form invites a write into the
tables: a connection string says where llmbroker keeps its own state, not that
the state is the host's to edit, and content the host owns arrives through the
port ([`sync-merge.md`](sync-merge.md#the-partition-a-sync-touches-only-what-a-sync-wrote)).

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

# Backends and wiring

The three pluggable ports, how one source parameter becomes them, the broker's
lifecycle, and the DB schema policy. What the journal holds and how it is read
is [`journal.md`](journal.md). The cross-cutting rules this file elaborates are
in [`../invariants.md`](../invariants.md).

## The three ports

Every host plugs in up to three backends; only the registry is required.

| Port | Contract | Default (zero-dependency) | What it is |
|---|---|---|---|
| **config** | `RegistryProtocol` | a file registry (`.toml`/`.json`) | where LLM configurations are stored — the merged lineup |
| **secrets** | `SecretsProtocol` | env vars, with an optional `.env` fallback | how `api_key_ref` names resolve to real keys |
| **store** | `StoreProtocol` | a `store/` directory | the append-only call journal plus the admin disabled-verdict map |

The store is the only storage llmbroker owns and writes.

**Where each kind lives.** Contracts live in `llmbroker.protocols` — implement
one to add a custom backend; they are not part of the top-level surface.
Zero-dependency implementations live in `llmbroker.standalone` and are
re-exported on the top-level package: a config file, env-var secrets, a
file-backed store, no integration code. Dependency-carrying backends are one
subpackage per driver, each re-exporting its own classes from its own
`__init__.py` — they cannot live on the top-level package the way `standalone`
does, since that would force the optional driver import on a bare
`import llmbroker`, and importing the subpackage is the dependency declaration.

Internally each SQL/document backend is one storage `Driver` behind one shared
port implementation written once against the `Driver` protocol; adding a new DB
backend is one driver file. A custom backend outside the package implements
either one `Driver`, to reuse the shared ports, or a full port protocol
directly.

**The default secrets backend reads a `.env` file, without a dependency.** A
broker whose config source is a file defaults to that file's sibling `.env` as a
fallback consulted only when the real environment has no such variable — the
exported value always wins, and a missing file is simply an empty fallback. The
parser is stdlib-only (`KEY=VALUE` lines, `#` comments, no interpolation) and a
malformed line is skipped rather than fatal. An unfilled `KEY=` line counts as
absent (invariant 21): the skeleton the `env` command prints is all unfilled
lines, and each must leave its model inactive rather than route with no
credential. The file is re-read when it changes, so a key filled in while the
broker runs takes effect on the next resync. A DB source or an explicit secrets
object is unaffected.

## Choosing a source

**No source is a source — the default installation.** A bare `Broker()` runs the
curated free pool: the merged lineup in the home directory (seeded on first use
from the fetched, cached or bundled preset), keys from the environment with the
working directory's `.env` behind them, the journal in the home directory too.
The lineup file is a file registry like any other, so the refresh applies to it
unchanged. Where nothing is writable, the lineup and the journal live in process
memory for that run — the broker still routes, it just remembers nothing between
runs.

The journal is machine-global here because keys in this mode come from the
environment, so the quota it tracks really is one pool; a journal scattered per
working directory would make every run rediscover the same 429. Two projects on
genuinely different keys are already separated, since 429 and dead-key evidence
scope by key hash, and a project wanting full isolation passes its own home.

**Source-parameter dispatch.** The broker's first positional argument is the
data source; passing a plain string or path dispatches on its form: a config
file extension gives a file registry with env-var secrets; a sqlite path or URL
gives sqlite backing all three ports from one file; a postgres or mongodb URL
gives that driver backing all three ports. An unrecognized form raises a clear
error naming the accepted ones; a missing extra raises an actionable install
message. Each backend package is imported lazily so a bare `import llmbroker`
never pulls in a driver. Explicit port arguments always win over whatever the
source would have supplied — passing an already-constructed protocol object as
the first argument skips dispatch entirely. The single-port secrets backends
stay override-only.

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
- **registry** — the merged lineup. Global, no scope dimension, no learned data,
  and nothing but `sync` writes it. It stores no ordering (invariant 3), so a
  backend author must not assume the load order means anything.
- **disabled** — the admin verdict map, a flat name-to-boolean mapping. Written
  only by the disable verb or seeded with missing names by a sync or by
  provisioning.
- **secrets** — a flat ref-to-value store keyed by ref alone. No scope column:
  the broker folds the scope into the ref string as a prefix, and the value is a
  single opaque scalar, so JSON buys nothing.

There is no state or summaries table — shared cooldowns and learned quality
derive entirely from the calls journal.

**Host migration coexistence.** The package ships a predicate for Alembic's
`include_object` hook that excludes every `llmbroker_*` object from
autogenerate, carrying zero Alembic dependency: it inspects the object name
only.

## Secret naming

Each managed-secret backend uses a deterministic, namespaced path so secrets
written by llmbroker are identifiable and isolated from the rest of the account.
Neither has a user or scope parameter — the ref is the whole identity, already
carrying any scope prefix the broker added.

- **AWS Secrets Manager** — the secret name is a configurable prefix followed by
  the ref, defaulting to an `llmbroker/` prefix. Secrets created by llmbroker
  carry an `llmbroker` tag for independent enumeration and cleanup.
- **HashiCorp Vault** — the KV v2 engine, under an `llmbroker/` path.

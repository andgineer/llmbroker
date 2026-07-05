# llmbroker — current architecture

`llmbroker` routes LLM calls over a configured pool of endpoints
(`base_url + model + api_key`). When an endpoint returns 429 or 503, the broker
cools it down and retries the next available one. The caller gets a result or an
exception — never silence.

---

## The three pluggable backends

Every host plugs in up to three backends; only the registry is required:

| Backend | Contract | Default (zero-dependency) | What it is |
|---|---|---|---|
| **config** | `RegistryProtocol` | `Registry(path)` (file: `.toml`/`.json`) | where LLM configurations are stored — a pure mirror of a preset, see "Provider seeding" |
| **secrets** | `SecretsProtocol` | `Secrets()` (env vars) | how `api_key_ref` names resolve to real keys |
| **store** | `StoreProtocol` | `FileStore(path)` (`store/` dir) | append-only call journal plus the admin disabled-verdict map; see [`optimizer.md`](optimizer.md) |

The store is the only storage llmbroker owns and writes: the append-only call
journal, the admin disabled-verdict map, and any future operational data
(aggregates, per-user settings).

**Where each kind lives:**
- **Contracts** (`RegistryProtocol`, `SecretsProtocol`, `StoreProtocol`, …) live in
  `llmbroker.protocols` — implement one to add a custom backend. They are not part of
  the top-level surface.
- **Zero-dependency implementations** that work without any external backend live in
  `llmbroker.standalone` and are re-exported for convenience: construct them directly as
  `llmbroker.Registry`, `llmbroker.Secrets`, `llmbroker.FileStore` (plus variants
  `DictSecrets`, `InMemoryStore`). This is the simplest usage — a config file,
  env-var secrets, a file-backed store, no integration code.
- **Dependency-carrying backends** are named modules imported explicitly
  (`llmbroker.sqlite.registry.Registry`, …), one subpackage per driver
  (`llmbroker.sqlite`, `llmbroker.postgres`, `llmbroker.mongodb`, `llmbroker.aws`,
  `llmbroker.vault`) — the subpackage's own `__init__.py` carries no code, so
  `llmbroker.sqlite.Registry` is not importable; only the exact module path is.
  Importing the submodule is the dependency declaration: a bare `import llmbroker`
  never pulls in a driver. Internally, each of sqlite/postgres/mongodb is one
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
  loads the registry into the pool, raising if it is empty (call `sync(preset)`
  first). Called automatically by `chat`, `snapshot`, `get`, `count`, and
  `__aenter__`; call explicitly for eager fail-fast startup.
- `sync(preset)` — mirrors a preset into the registry: add new entries, update
  existing ones, delete entries absent from the preset. The only registry write
  path; there is no `add`/`update`/`remove`. See "Provider seeding" below.
- Plain `KeyError` signals a missing LLM everywhere in the public API.
- **Scoping** — an opaque `scope: str | None` string on the broker prefixes secret
  refs (own key, falling back to the shared ref) and attributes journal rows; the
  registry and everything the optimizer learns are always global. See "Per-user
  scoping" below.

### Batteries

| Backend | Implemented |
|---|---|
| Registry | `Registry(path)` (file, `.toml`/`.json`), `llmbroker.sqlite.registry.Registry`, `llmbroker.postgres.registry.Registry`, `llmbroker.mongodb.registry.Registry` |
| Secrets | `Secrets()` (env), `DictSecrets(mapping)` (test double), `llmbroker.sqlite.secrets.Secrets`, `llmbroker.postgres.secrets.Secrets`, `llmbroker.mongodb.secrets.Secrets`, `llmbroker.aws.secrets.Secrets`, `llmbroker.vault.secrets.Secrets` |
| Store | `FileStore(path)` (day-split journal + YAML disabled map), `InMemoryStore()`, `llmbroker.sqlite.store.Store`, `llmbroker.postgres.store.Store`, `llmbroker.mongodb.store.Store` |

### CLI

- `python -m llmbroker env <config>` — emit a `.env` skeleton of `api_key_ref` names,
  in file (`llms` declaration) order, with each one's `help` text
  (see "Key acquisition help" above). Onboarding is folded into this command rather
  than a separate `setup`/`status` command, to keep the CLI surface small.
- `python -m llmbroker preset <name>` — print a curated preset TOML to stdout (redirect to save: `preset freetier > freetier.toml`)
- `python -m llmbroker sync <preset> <db>` — mirror a preset TOML into a DB
  registry, `<db>` accepting any source dispatch form (sqlite path/URL,
  `postgresql://…`, `mongodb://…`); a DB-init CLI touchpoint for the same
  `sync(preset)` the broker exposes.

### DB schema

Every DB backend self-manages its schema via `ensure_schema`: idempotent, checked
once per driver instance before any operation, version-aware. Every
table/collection is `llmbroker_`-prefixed so the host's migration tool can ignore
them by prefix. Single-known-installation policy: `ensure_schema` creates the
current shape fresh when no version marker exists, and raises an actionable
`RuntimeError`/error on any other version mismatch — there is no in-place
`ALTER`-based migration path; upgrading means dropping the `llmbroker_*`
tables/collections and restarting (export registry/secrets/calls first if needed).

**The table schema is not a public contract.** A host may query `llmbroker_calls`
or the other tables directly, but at its own risk — column names and shapes may
change between releases without notice. The supported read surface is
`snapshot()` (raw per-model facts + metrics); hosts that need more should read
through a `QueryableStoreProtocol`/`DisabledMapProtocol` backend, not the
raw table.

Four tables/collections exist: **registry**, **secrets**, **disabled** (admin
verdicts, seeded with model names at `sync`), and **calls** (the journal). There
is no state or summaries table — shared cooldowns and learned quality derive
entirely from the calls journal (see [`optimizer.md`](optimizer.md)).

- **SQLite** tracks version via `PRAGMA user_version`.
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
  (see [`optimizer.md`](optimizer.md)).
- **Registry** (`llmbroker_registry`) — hybrid: `name`, `base_url`, `model`,
  `api_key_ref` stay columns (identity, plus stable human-meaningful config);
  nested/open-ended per-LLM config (e.g. `parallel`) lives in the `metadata`
  JSON column. The registry is global (no scope column) and a pure mirror of a
  preset (see "Provider seeding") — nothing but `sync` writes it, and it holds
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
would keep spending that shared quota on worse answers. The next `sync(preset)`
at any deployment already running the old entry deletes it — its ratings and
verdicts are gone with it, since nothing survives outside the journal/disabled
map that `sync` never touches (see "Provider seeding" below). See
[`freetier-providers.md`](freetier-providers.md) for how the curated free-tier
preset specifically is kept current.

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

There is no background key re-resolve loop: a key added to the environment
after startup takes effect at the next `ensure_pool()` call (fresh process, or
an explicit re-provision) or immediately if the host calls `sync(preset)` again
(it re-bootstraps any newly resolvable secrets) — never via a polling task.

---

## Provider seeding

The preset file is the only source of model definitions; the registry is its
pure mirror. Seeding is an explicit call, never implicit at construction —
implicit seed-on-start is unsound in a cluster, since every node would reconcile
the registry against its own local copy and diverging copies would flip-flop it.

```python
llms = llmbroker.AsyncBroker(
    registry=llmbroker.sqlite.registry.Registry("broker.db"),
    secrets=llmbroker.sqlite.secrets.Secrets("broker.db"),
)
await llms.sync(llmbroker.Registry(".deploy/llms.toml"))  # once, e.g. at deploy
await llms.ensure_pool()   # eager init at startup
```

`sync(preset)` is a total mirror: add new entries, update existing ones, delete
entries absent from the preset — nothing is lost by a delete, since keys live in
the secrets store, learned state derives from the journal, and admin verdicts
live in the store disabled map (a model returning to the preset is simply
re-added, and its old ratings and verdict resurface). Refusing a `model`-identity
change under an existing entry name is a synchronous error — entry identity is
immutable, a model bump must be a new entry name; this protects the binding
between a model's learned quality stats and its name. There is no other model
CRUD — no `add`/`update`/`remove` — `sync(preset)` is the only registry write
path. Provisioning against an empty registry fails fast, telling the caller to
call `sync(preset)` first.

`sync` also bootstraps secrets: for each provider config whose `api_key_ref`
cannot be resolved by the configured `secrets=` backend, it tries
`llmbroker.Secrets()` (env vars) and, if found, persists the value via
`secrets.set()`. Existing secrets are never overwritten — admin-edited values
win. It also seeds the store disabled map with any missing model names
(`disabled: false`), never touching existing verdict values.

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

---

## Secret naming conventions

Each managed-secret backend uses a deterministic, namespaced path so secrets written by
llmbroker are identifiable and isolated from the rest of the account. Neither backend
has a user/scope parameter — `ref` is the whole identity, already carrying any scope
prefix the broker added (see "Per-user scoping" above).

### AWS Secrets Manager (`llmbroker.aws.secrets.Secrets`)

Secret name in Secrets Manager: `{prefix}{ref}` — `prefix` defaults to `"llmbroker/"`.
Secrets created via `set()` carry the tag `{"Key": "llmbroker", "Value": "1"}` for
independent enumeration and cleanup.

### HashiCorp Vault (`llmbroker.vault.secrets.Secrets`)

KV v2 engine. KV path: `llmbroker/{ref}`.

---

## Not yet implemented

| Feature | Phase |
|---|---|
| LLM-as-judge quality scoring | P5 |

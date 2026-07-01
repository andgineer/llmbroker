# llmbroker — current architecture

`llmbroker` routes LLM calls over a configured pool of endpoints
(`base_url + model + api_key`). When an endpoint returns 429 or 503, the broker
cools it down and retries the next available one. The caller gets a result or an
exception — never silence.

---

## The four pluggable backends

Every host plugs in up to four backends; only the registry is required:

| Backend | Contract | Default (zero-dependency) | What it is |
|---|---|---|---|
| **config** | `RegistryProtocol` | `Registry(path)` (file: `.toml`/`.json`) | where LLM configurations are stored |
| **secrets** | `SecretsProtocol` | `Secrets()` (env vars) | how `api_key_ref` names resolve to real keys |
| **state store** | `StateStoreProtocol` | absent — single-process only | persists cooldown state between requests (any stateless server, not just clusters) |
| **telemetry** | `TelemetryProtocol` | `Telemetry()` (Python logging) | append-only call journal |

**Where each kind lives:**
- **Contracts** (`RegistryProtocol`, `SecretsProtocol`, …) live in `llmbroker.protocols` —
  implement one to add a custom backend. They are not part of the top-level surface.
- **Zero-dependency implementations** that work without any external backend live in
  `llmbroker.standalone` and are re-exported for convenience: construct them directly as
  `llmbroker.Registry`, `llmbroker.Secrets`, `llmbroker.Telemetry` (plus variants
  `DictSecrets`, `NoTelemetry`, `JsonlTelemetry`). This is the simplest usage — a config
  file, env-var secrets, logging telemetry, no integration code.
- **Dependency-carrying backends** are submodules imported explicitly
  (`llmbroker.sqlite.Registry`, …), one subpackage per driver (`llmbroker.sqlite`, and
  `llmbroker.postgres`/`redis`/… as they ship). Importing the submodule is the dependency
  declaration: a bare `import llmbroker` never pulls in a driver.

---

## What is implemented

### Core

- `AsyncBroker` — async engine. One `asyncio.Queue` slot per LLM; at most one
  in-flight request per LLM. On 429/503: cooldown via `loop.call_later` re-enqueue.
  Lazy start (no `start()` call required). `aclose()` / `async with` lifecycle.
  LLMs are identified by name; access is always by name.
- `Broker` — synchronous wrapper over `AsyncBroker` on a dedicated background
  event-loop thread. First-class shipped surface, not an afterthought.
- `optimize` parameter shape (`bool | Optimizer`) is locked. `optimize=True` (default)
  activates the `Optimizer` component: adaptive per-LLM cooldown delay, OFFLINE/PROBING
  FSM, and alert emission. See [optimizer.md](optimizer.md) for the behavior spec.
- `ensure_pool()` — lazy idempotent pool initializer with double-checked locking.
  Applies the constructor `seed=` source first, then loads the registry into the
  pool. Called automatically by `chat`, `snapshot`, `get`, `count`, `add`,
  `update`, `remove`, and `__aenter__`; call explicitly for eager fail-fast startup.
- **Mutability contract** — `add` is create-only (raises `ValueError` if the name
  already exists); `update` is modify-only (raises `KeyError` if the name is
  absent). Plain `KeyError` signals a missing LLM everywhere in the public API;
  `ValueError` signals a duplicate on `add`. No domain exception subclasses.
- **State merging** — when a `StateStoreProtocol` backend is configured, `state()`
  on an `AsyncLLM` handle and `snapshot()` on the broker both prefer the stored
  state over the in-process state. The store read is batched (one call
  for `snapshot`, one call per `state()` invocation); `InMemoryState` is the
  fallback when the store has no entry for that LLM.
- `SeedPolicy` enum (`IF_EMPTY` / `ADD` / `MIRROR`) — controls how the constructor
  `seed=` source reconciles the registry on first `ensure_pool`. See "Provider
  seeding" below.
- **Per-user scoping** — a single `user_id` on the broker scopes every backend call to
  one tenant; all backends scope records exactly to it (`None` = unscoped /
  single-tenant). See "Per-user scoping" below.

### Batteries

| Backend | Implemented |
|---|---|
| Registry | `Registry(path)` (file, `.toml`/`.json`), `llmbroker.sqlite.Registry`, `llmbroker.postgres.Registry`, `llmbroker.mongodb.Registry` |
| Secrets | `Secrets()` (env), `DictSecrets(mapping)` (test double), `llmbroker.sqlite.Secrets`, `llmbroker.postgres.Secrets`, `llmbroker.mongodb.Secrets`, `llmbroker.aws.Secrets`, `llmbroker.vault.Secrets` |
| State store | `llmbroker.sqlite.StateStore` (single-machine), `llmbroker.redis.StateStore`, `llmbroker.postgres.StateStore`, `llmbroker.mongodb.StateStore` (cross-process) |
| Telemetry | `Telemetry()` (log), `NoTelemetry()`, `JsonlTelemetry(path)`, `llmbroker.sqlite.Telemetry`, `llmbroker.postgres.Telemetry`, `llmbroker.mongodb.Telemetry` |

### CLI

- `python -m llmbroker env <config>` — emit a `.env` skeleton of `api_key_ref` names
- `python -m llmbroker preset <name>` — print a curated preset TOML to stdout (redirect to save: `preset freetier > freetier.toml`)

### DB schema

Every DB backend self-manages its schema via `ensure_schema`: idempotent, called on
first use, version-aware. Every table/collection is `llmbroker_`-prefixed so the
host's migration tool can ignore them by prefix.

- **SQLite** tracks version via `PRAGMA user_version`.
- **Postgres** tracks version via a single-row `llmbroker_schema_version` table
  (no PRAGMA in Postgres). The caller owns the `asyncpg.Pool` lifecycle; `aclose()`
  on every class is a no-op by design so the pool is not closed prematurely.
- **MongoDB** tracks version via a document in `llmbroker_schema_version`. The caller
  owns the Motor database handle. `user_id: None` is stored explicitly in every
  document (not as an absent field) so MongoDB null participates correctly in unique
  indexes.
- **Redis** stores one hash per `(user_id)` scope under `llmbroker_state:*` keys;
  keys have no TTL in v1 (cooling entries accumulate indefinitely — acceptable for
  the initial release).

#### Columns vs. JSON

A field earns a dedicated column only if it appears (or realistically will) in a
`WHERE`/`JOIN`/`ORDER BY`/`GROUP BY`/aggregate; everything else is payload and
lives in a single JSON column (JSONB on postgres, TEXT on sqlite; native
document/hash on mongo/redis) keyed by the row's identity. This is a hybrid, not
"JSON everywhere" — identity and queried fields stay first-class columns and keep
their indexes.

Per table:

- **Telemetry** (`llmbroker_calls`) — unchanged: `id`, `llm_name`, `called_at`,
  `user_id`, `status` are queried/indexed columns; open-ended provider extras
  already live in the `usage_extra` JSON column.
- **State** (`llmbroker_state`) — a single JSON document per `(llm_name,
  user_id)`; never queried by an inner field, ephemeral, rebuilt from traffic.
  A schema-version bump for this table is a **drop-based** migration (the old
  rows are disposable) — safe because state is a live cache, not durable data.
- **Registry** (`llmbroker_registry`) — hybrid: `name`, `base_url`, `model`,
  `api_key_ref`, `user_id` stay columns (identity, plus stable human-meaningful
  config); nested/open-ended per-LLM config (e.g. `rate_limit`) lives in one
  `metadata` JSON column. Because registry rows are durable, a schema-version
  bump for this table is an **additive** migration (`ALTER TABLE ... ADD
  COLUMN`) that preserves existing rows rather than dropping them.
- **Secrets** (`llmbroker_secrets`) — unchanged: `value` is a single opaque
  scalar with no sub-structure, so JSON buys nothing.

`ensure_schema`, when it finds the stored version marker stale, applies both
kinds of migration in one pass in a fixed order — drop-based (state) before
table creation, additive (registry) after — so a database upgrading from any
older version reaches the current shape in a single call. On sqlite this whole
read-marker → migrate → bump-marker sequence runs inside one `BEGIN IMMEDIATE`
transaction, since multiple OS processes can share one sqlite file and an
in-process lock alone cannot serialize them; postgres gets the same guarantee
for free from `CREATE`/`ALTER TABLE`'s table-level lock.

`LLMState` and `LLMConfig` (`src/llmbroker/models.py`) are the typed dataclass
boundary for the JSON payloads — `LLMState.to_dict()`/`from_dict()` round-trip
the state document (with a generic `extra` field for forward-compatible keys),
and `LLMConfig.rate_limit` (an optional `RateLimit` of rpm/rpd/tpm/tpd) is the
first structured field persisted through the registry's `metadata` column.

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

---

## Key acquisition help

A config source may carry, per `api_key_ref`, a short markdown string describing
how to obtain that key (a link plus a step or two). It is keyed by the env-var
name, not by LLM, because one key is typically shared by several LLMs. The format
is deliberately just markdown — no structured provider/free/url fields to keep in
sync.

The same data feeds two consumers:

- the `env` CLI prints each string as a comment above its variable, prefixed with
  the variable name so the comment is unambiguously tied to its key;
- a host can pull the strings to render its own setup UI.

Surfacing it is an **optional registry capability**, independent of the broker. A
registry that has the metadata exposes it; one that does not simply omits the
capability. Hosts query whichever registry they hold — there is no requirement
that the broker was constructed with a seed, and no coupling between obtaining the
help and routing.

---

## Provider seeding

The broker seeds on first use via the constructor `seed` parameter. Pass any
`RegistryProtocol` source (e.g. a TOML file) and a `SeedPolicy`:

```python
llms = llmbroker.AsyncBroker(
    registry=llmbroker.sqlite.Registry("broker.db"),
    secrets=llmbroker.sqlite.Secrets("broker.db"),
    seed=llmbroker.Registry(".deploy/llms.toml"),
    seed_policy=llmbroker.SeedPolicy.ADD,
)
await llms.ensure_pool()   # eager init at startup
```

`SeedPolicy` controls reconciliation:

| Policy | Behavior |
|---|---|
| `IF_EMPTY` (default) | Seeds only when the registry is empty; no-op on restart if providers are present |
| `ADD` | Adds providers absent by name; never removes or updates existing entries |
| `MIRROR` | Keeps the registry identical to the seed source: adds new, updates changed, removes absent |

When seeding, the broker also bootstraps secrets: for each provider config whose
`api_key_ref` cannot be resolved by the configured `secrets=` backend, the broker
tries `llmbroker.Secrets()` (env vars) and, if found, persists the value via
`secrets.set()`. Existing secrets are never overwritten — admin-edited values win.
Once the secrets store is populated, env vars are not consulted again at runtime.

`IF_EMPTY` with a non-empty registry exits early without re-seeding secrets — by
design: a non-empty registry means secrets were already bootstrapped during the
initial seed. `ADD` and `MIRROR` always attempt to fill missing secrets on every
startup.

---

## Per-user scoping (multi-tenancy)

A multi-user host can give each end user its own LLM API keys (and optionally its
own set of LLMs) over one shared infrastructure (one DB, one Redis, one Vault). The
driving constraint is correctness of cooldown state: a cooldown is the health of an
*API key*, and keys are per-user — so key-scope and state-scope must move together,
or one user's 429 would cool an LLM for everyone.

- **One knob.** Tenancy is selected by a single `user_id` supplied to the broker;
  the broker threads it into every backend call. There is no per-backend tenancy wiring
  to keep in sync, so key-scope and state-scope cannot drift apart.
- **`user_id` is request-scoped, not infrastructure.** Backends (registry, secrets,
  state store, telemetry) are constructed once as app-lifetime infrastructure and
  shared; in a stateless server the broker is cheap and constructed *per request*
  with that request's `user_id`.
- **A broker instance is one tenant's view.** The broker never multiplexes users
  internally — resolved keys and the per-LLM queue are single-user. `user_id` absent
  (the default) is exactly the single-tenant behavior, untouched.
- **`None` means unscoped / single-tenant** and is a legitimate value. The broker
  passes whatever it has — `None` or an id — and the batteries decide what to do
  with it.
- **Uniform, exact scoping across all batteries.** Registry, secrets, state store,
  and telemetry all scope records to exactly the `user_id` passed: a scoped load
  returns only that tenant's rows, an unscoped load only unscoped rows. There is no
  "shared ∪ the user's own" merging — mixing unscoped and named tenants in one query
  produces subtle seeding bugs that cannot be made correct.
- **Exception — retention purge is cross-tenant.** Purging old call records is an
  **administrative** maintenance action over the whole journal, not a per-tenant read,
  so it deliberately ignores `user_id` and drops every user's rows older than the
  cutoff. It is the one telemetry operation not scoped to one tenant.
- **Per-user is a parameter, not a subclass.** The same battery classes gain a
  nullable user scope; there is no `PerUser*` variant and the battery matrix gains no
  "× tenancy" axis. Pure batteries with no notion of users (file registry, env
  secrets, log/none telemetry) accept the scope and ignore it.
- **Seeding stays shared.** The constructor `seed=` catalog is read unscoped (shared
  defaults) but written into the registry under the tenant's `user_id`, so every
  tenant bootstraps from the same curated source into its own scoped rows.
- **Uniqueness is per tenant.** The same LLM name (or secret ref) is allowed across
  different users but remains unique within one user — and within the unscoped
  single-tenant bucket.
- **Optional paranoia guard.** A secrets battery may be constructed to *require* a
  user scope, raising `UserScopeError` if it is ever asked to resolve or set with no
  user (e.g. auth silently yielded none). It is opt-in; normal use relies on always
  passing a real id.

---

## Shared cooldown across processes

When a `StateStoreProtocol` backend is configured, a cooldown set by one process
must be honored by every other process backed by the same store: when process A
cools an LLM after a 429/503, process B skips that LLM at its next selection
point instead of earning a redundant rate-limit error.

The shared state is consulted lazily, just before an acquired LLM is used — there
is no background timer or polling, and an idle broker performs zero store reads.
To stay cheap under load, each process caches the shared state for a short
bounded window, so at most one read happens per window regardless of request
volume. The read is scoped to the broker's `user_id` like all other state.

When a shared cooldown is detected, the process mirrors it into its own state and
defers the LLM for the remaining window, so it does not re-probe until the
cooldown expires. Any quality-fail history the local process has accumulated is
preserved, never lowered by the shared count.

Two consequences are accepted by design:

- **Bounded staleness.** Within the cache window a process may not yet see a
  cooldown another process just wrote, so it can still earn one redundant 429
  before converging. The cost is a single wasted call, not a correctness failure.
- **Fail-open on store errors.** If the shared-state read fails, the process
  proceeds as if no shared cooldown applied rather than blocking the request — at
  worst it earns a 429 it could have avoided.

---

## Secret naming conventions

Each managed-secret backend uses a deterministic, namespaced path so secrets written by
llmbroker are identifiable and isolated from the rest of the account.

### AWS Secrets Manager (`llmbroker.aws.Secrets`)

| `user_id` | Secret name in Secrets Manager |
|-----------|-------------------------------|
| `None`    | `{prefix}{ref}`               |
| `"alice"` | `{prefix}{ref}/alice`         |

`prefix` defaults to `"llmbroker/"`.  Secrets created via `set()` carry the tag
`{"Key": "llmbroker", "Value": "1"}` for independent enumeration and cleanup.

### HashiCorp Vault (`llmbroker.vault.Secrets`)

KV v2 engine.

| `user_id` | KV path                          |
|-----------|----------------------------------|
| `None`    | `llmbroker/{ref}`                |
| `"alice"` | `llmbroker/users/alice/{ref}`    |

---

## Not yet implemented

| Feature | Phase |
|---|---|
| LLM-as-judge quality scoring | P5 |

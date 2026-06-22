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
  is a reservation — the control loop does not run until Phase 4. In the current
  version `optimize=True/False` has no effect on routing.
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
| Registry | `Registry(path)` (file, `.toml`/`.json`), `llmbroker.sqlite.Registry` (CRUD-capable) |
| Secrets | `Secrets()` (env), `DictSecrets(mapping)` (test double), `llmbroker.sqlite.Secrets` |
| State store | `llmbroker.sqlite.StateStore` (single-machine: preserves cooldown across restarts) |
| Telemetry | `Telemetry()` (log), `NoTelemetry()`, `JsonlTelemetry(path)`, `llmbroker.sqlite.Telemetry` |

### CLI

- `python -m llmbroker env <config>` — emit a `.env` skeleton of `api_key_ref` names
- `python -m llmbroker preset <name>` — print a curated preset TOML to stdout (redirect to save: `preset freetier > freetier.toml`)

### DB schema

`llmbroker.sqlite` self-manages its tables via `ensure_schema`: it creates them on
first use and is **version-aware** — it tracks a schema version (`PRAGMA
user_version`) so later releases can hang additive, data-preserving migrations off
that marker. No migrations are needed yet (the initial schema is still version 1);
the upgrade path is reserved, not exercised. Every DB object is `llmbroker_`-prefixed
so the host's migration tool can ignore them by prefix.

### Host migration coexistence

`llmbroker.integrations.alembic.include_object` — a predicate for Alembic's `include_object`
hook that excludes every `llmbroker_*` object from autogenerate. Zero Alembic
dependency: the hook inspects the object name only.

---

## Preset distribution

Curated LLM lists live in `presets/` at the repository root — not in the wheel.
A list update is a plain commit, independent of any package version. The
`preset <name>` CLI command fetches from the repository default branch:

```
https://raw.githubusercontent.com/andgineer/llmbroker/main/presets/<name>.toml
```

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

## Not yet implemented

| Feature | Phase |
|---|---|
| `StateStore` cross-node backends (redis, postgres, mongodb) | P3 |
| Optimizer control loop (delay tuning, routing, offline/probe FSM) | P4 |
| LLM-as-judge quality scoring | P5 |

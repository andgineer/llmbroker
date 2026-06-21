# llmbroker — current architecture

`llmbroker` routes LLM calls over a configured pool of endpoints
(`base_url + model + api_key`). When an endpoint returns 429 or 503, the broker
cools it down and retries the next available one. The caller gets a result or an
exception — never silence.

---

## Four-port model

Every host wires up to four things; only the registry is required:

| Port | Interface | Default battery | What it is |
|---|---|---|---|
| **config** | `RegistryProtocol` | `Registry(path)` (file: `.toml`/`.json`) | where LLM configurations are stored |
| **secrets** | `SecretsProtocol` | `Secrets()` (env vars) | how `api_key_ref` names resolve to real keys |
| **state store** | `StateStoreProtocol` | absent — single-process only | persists cooldown state between requests (any stateless server, not just clusters) |
| **telemetry** | `TelemetryProtocol` | `Telemetry()` (Python logging) | append-only call journal |

**Naming convention — one rule for all ports:**
- Bare name = the zero-dep default battery you construct directly (`Registry`, `Secrets`, `Telemetry`)
- Descriptive prefix = a zero-dep variant (`DictSecrets`, `NoTelemetry`, `JsonlTelemetry`)
- `Protocol` suffix = the structural interface to implement (`RegistryProtocol`, `TelemetryProtocol`, …)
- Submodule = a battery that carries an external dependency (`llmbroker.sqlite.Registry`, `llmbroker.sqlite.Telemetry`)

**Battery classification — one rule:** if a battery has no external dependency it is
top-level (`import llmbroker` only); if it does it is a submodule you import
explicitly (`import llmbroker.sqlite`). There is no list to memorize: the submodule
import is the dependency declaration.

---

## What is implemented (v0.0.5)

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

### Batteries

| Port | Implemented batteries |
|---|---|
| Registry | `Registry(path)` (file, `.toml`/`.json`), `llmbroker.sqlite.Registry` (CRUD-capable) |
| Secrets | `Secrets()` (env), `DictSecrets(mapping)` (test double), `llmbroker.sqlite.Secrets` |
| State store | `StateStoreProtocol` seam is defined; **no backends** (Phase 3) |
| Telemetry | `Telemetry()` (log), `NoTelemetry()`, `JsonlTelemetry(path)`, `llmbroker.sqlite.Telemetry` |

### CLI

- `python -m llmbroker env <config>` — emit a `.env` skeleton of `api_key_ref` names
- `python -m llmbroker sync <config> --into sqlite:<path> [--policy mirror|add|if_empty]` — reconcile a TOML into a sqlite DB
- `python -m llmbroker preset <name>` — **Phase 2, not yet implemented**

### DB schema

`llmbroker.sqlite` self-manages its tables via `ensure_schema`: creates on first
use, applies additive data-preserving migrations on upgrade. Every DB object is
`llmbroker_`-prefixed so the host's migration tool can ignore them by prefix.

### Host migration coexistence

`llmbroker.alembic.include_object` — a predicate for Alembic's `include_object`
hook that excludes every `llmbroker_*` object from autogenerate. Zero Alembic
dependency: the hook inspects the object name only.

---

## Preset distribution

Curated LLM lists live in `presets/` at the repository root — not in the wheel.
A list update is a plain commit, independent of any package version. The
`preset <name>` CLI command (Phase 2) fetches from the repository default branch:

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

The `python -m llmbroker sync` CLI command performs the same reconciliation offline
(without a running application) and is useful for ops workflows.

---

## Not yet implemented

| Feature | Phase |
|---|---|
| `preset` CLI command (URL-fetch from repo) | P2 |
| `StateStore` backends (redis, postgres, mongodb) | P3 |
| Optimizer control loop (delay tuning, routing, offline/probe FSM) | P4 |
| LLM-as-judge quality scoring | P5 |

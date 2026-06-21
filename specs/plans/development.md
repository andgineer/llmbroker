# llmbroker — development plan

## Goal

`llmbroker` is a standalone, host-agnostic LLM-provider broker. It provides one
thing: LLM access over a *pool of configured endpoints* (each a
`(base_url, model, api_key)` triple), rotating away from ones that are momentarily
unavailable (429/503), and accumulating enough signal to decide which to drop or add.

Three design targets:

- **Dead-simple typical use.** Fetch a curated pool file, put keys in env vars,
  write one constructor line. A typical host writes **no integration code** and
  **never puts a secret in source**.
- **Full universality.** Any storage, any secret backend, single-process or
  clustered — each is a shipped *battery*; the rare host with a non-standard
  requirement implements one small port.
- **Self-tuning.** A background optimizer reads telemetry (per LLM *and per
  operation*) and **acts**: adjusts cooldowns, offlines and re-probes bad LLMs,
  routes each operation to the LLMs that empirically handle it best. A human is
  interrupted only by what only a human can fix (pool under-provisioned, API key
  dead).

---

## Status

Phase 1 shipped as v0.0.3 (June 2026), stabilised as v0.0.4 and v0.0.5. The package
is in its own repository with its own `pyproject.toml`. Phases 2–5 are described
below.

Invariants that hold for every future phase: zero host-specific imports, every DB
object `llmbroker_`-prefixed, `ensure_schema` as sole schema owner, Alembic
coexistence hook. Any coupling to a specific host application is a defect.

---

## The mental model

A host wires up to four things; only the first is mandatory, the rest have
working defaults:

| Concept | Port (interface) | Required? | Default battery | What it is |
|---|---|---|---|---|
| **config** | `RegistryProtocol` | **yes** | — | where the LLM configuration is stored / loaded |
| **secrets** | `SecretsProtocol` | no | `Secrets()` (env) | how `api_key_ref` references resolve to real keys |
| **shared state** | `SharedStateProtocol` | no — **opt-in, cluster only** | none (single process keeps state in-memory internally) | cross-instance sync of per-LLM live state (cooldown, fail count, offline) — supply it only to make several `llmbroker` copies agree |
| **telemetry** | `TelemetryProtocol` | no | `Telemetry()` (log) | append-only journal of calls — to see what happened and decide which LLMs to keep |

**`SharedState` is opt-in and exists only for clusters.** The broker always
keeps per-LLM live state (cooldown/fail/offline) in memory internally — that is a
private detail, not a user-facing port. You pass `shared_state=` *only* to share
that state across several `llmbroker` instances; there is deliberately no "local"
variant, because the absence of the parameter already means "single process,
nothing to coordinate". A database does not call for it — persisting ephemeral
cooldown for one process buys nothing (a stale cooldown after a restart is worse
than re-learning from a live 429). So the "DB" axis is purely `Registry` (config)
+ `Telemetry` (log); `SharedState` is orthogonal and only about multi-instance
sync.

**Naming convention.** The bare name is the **default concrete battery**, built
by direct construction — `llmbroker.Registry("llms.toml")` (file),
`llmbroker.Secrets()` (env), `llmbroker.Telemetry()` (log) — the `httpx.Client` /
`pathlib.Path` idiom (no factory functions, no classmethods). A *variant* of a
zero-dep battery gets a descriptive prefix: `DictSecrets`, `NoTelemetry`,
`JsonlTelemetry`. A **dependency** backend is `llmbroker.<backend>.<Port>`
(`llmbroker.sqlite.Registry`, `llmbroker.redis.SharedState`) — the submodule
namespace already says the backend, so there is no `SqliteRegistry` stutter. The
**interface** a custom backend implements is `<Port>Protocol` (`RegistryProtocol`,
`SecretsProtocol`, `SharedStateProtocol`, `TelemetryProtocol`). When a port has **capability
layers** (a minimal contract the broker needs plus a richer one a host admin UI or
the Optimizer needs), each layer is its own protocol named
`<Capability><Port>Protocol` — `MutableRegistryProtocol(RegistryProtocol)`,
`QueryableTelemetryProtocol(TelemetryProtocol)`. `Protocol` is the **invariant suffix** marking
"this is a structural interface to implement" — it reads as exactly that, never as a base
class to inherit; the capability is an ordinary adjective prefix on
the port noun. So the rule is uniform — every protocol ends in `Protocol`, never a bare
`MutableRegistry` (that would mix a suffix and a prefix scheme, and the bare names
are reserved for batteries anyway). The default telemetry `llmbroker.Telemetry()` is
Python `logging` so call data is never silently lost; `llmbroker.NoTelemetry()` is
the explicit opt-out.

**Why the bare name is the default battery, not the interface** (rejected
alternatives, so this is not re-litigated after extraction). Dependency-carrying
backends (sqlite/redis/postgres) **must** be submodules in any scheme — otherwise
`import llmbroker` pulls every optional driver — so the only open choice is naming
the *zero-dep defaults* and the *protocols*; the bare name `Registry` can go to one
or the other, not both.

- **Giving the protocol the bare name and prefixing the default** (`Registry` =
  Protocol, `FileRegistry`/`EnvSecrets`/`LogTelemetry` = defaults) is rejected on
  three counts. (1) It makes the most-guessable name a trap: a newcomer writes
  `llmbroker.Secrets()` expecting the env default and gets
  `TypeError: Protocols cannot be instantiated`. In the chosen scheme
  `llmbroker.Secrets()`/`Registry(path)`/`Telemetry()` *are* the obvious defaults.
  (2) It lengthens the **common** Rung-0 path (every host types `FileRegistry`) to
  tidy the **rare** custom-backend path (`RegistryProtocol`, seen only by someone
  writing a backend) — backwards: spend the short name where it is used most. It
  even makes the default longer than a dep backend (`FileRegistry` vs
  `sqlite.Registry`). (3) `<Port>Protocol` is the unambiguous Python marker for a
  structural interface — the suffix reads as "implement this", not "inherit this",
  which a bare `Registry` or a `Base`-suffixed name would blur.
- **Making the zero-dep defaults submodules too** (`llmbroker.toml.Registry`,
  `llmbroker.env.Secrets`) is rejected: it forces the 90% file/env/log user to learn
  a submodule for a stdlib-only thing and falsely implies a dependency. Symmetry for
  its own sake at the cost of the common case.

The payoff is **one rule across all ports — bare name = the sensible default**
(`Registry`/`Secrets`/`Telemetry`), learned once and applied everywhere; variants
get a descriptive prefix, the interface gets `Protocol`, a dep backend gets a submodule.

| Port interface | Reads / writes |
|---|---|
| `RegistryProtocol.load()` | `list[LLMConfig]` |
| `SharedStateProtocol.read()` / `.write(name, state)` | `dict[str, LLMState]` / saves one `LLMState` |
| `TelemetryProtocol.record(call)` | `Call` |
| `SecretsProtocol.resolve(ref)` | `str` (the resolved secret) |

The entity is the **`LLM`** — a configured `(base_url, model, api_key)` endpoint the
broker can call. The word **provider** is reserved for the *upstream vendor* (the
`base_url` host, e.g. Groq) — one provider can back several `LLM` entries
(different models), which is exactly why the config store is a `Registry`, not a
flat `Providers` list.

Each `LLM` is identified by an immutable **`name`** (the convention of `k8s
metadata.name` / `docker --name` — a human-authored unique id) used for every
reference (telemetry, shared state, routing) and as the `Broker` Mapping
key. The stored config (`LLMConfig`) holds an `api_key_ref` — an env-var name /
secret path, **never** the secret — and the broker resolves it via `Secrets` into a
**private** map (`_resolved_keys`) keyed by `name`; the resolved secret never lands
on a public object, so `LLMConfig` is safe to expose as-is.

---

## Quick Start (README draft)

`llmbroker` gives you one client over a *pool* of LLM endpoints and quietly
rotates away from any that are rate-limited or down. Start with a file and two
lines of code; reach for a database, a cluster, or tools only if you need them.

### Install and pick a pool of LLMs

```bash
pip install llmbroker
python -m llmbroker preset smart-freetier > llms.toml   # a curated LLM list; or `freetier`
python -m llmbroker env llms.toml > .env                # the API-key names to fill in
```
`preset` downloads one of the curated lists the project maintains — always the
latest, independent of your installed `llmbroker` version — so you don't research
endpoints yourself. `env` reads that list and writes a `.env` with the key *names*
it needs (no values) — fill them in. Keys live in env vars, never in `llms.toml`
and never in your code.

### The simplest way to use it

```python
import llmbroker

llms = llmbroker.Broker(registry=llmbroker.Registry("llms.toml"))
print(llms.ask("Summarize this receipt: ...").text)
```
That's the whole thing: ask a question, get an answer. The broker picks an LLM
from your pool, and if it's busy, tries another.

You can tag each call with what it's for, so the broker learns which LLMs do that
job best:
```python
llms.ask(prompt, operation="summary")
```

By default a call quietly waits out a short rate-limit instead of failing. If
you'd rather give up after a few seconds, pass `wait=`:
```python
try:
    llms.ask(prompt, wait=5)
except llmbroker.NoLLMAvailable:
    ...   # the whole llms pool was busy for 5 seconds
```

### The recommended way for real apps — async

For anything serving requests (FastAPI, agents, background workers) use the async
client. It's the same code with `await`:
```python
llms = llmbroker.AsyncBroker(registry=llmbroker.Registry("llms.toml"))
text = (await llms.ask("Summarize this receipt: ...")).text
```

### Letting the model call your functions (tools)

Pass your tool schemas to `chat` and let the shipped loop run the back-and-forth —
call the model, run the tool it asked for, send the result back, repeat — until it
returns a final answer:
```python
final = llmbroker.run_tool_loop(llms, messages, tools=schemas, dispatch=my_tools)         # sync
final = await llmbroker.arun_tool_loop(llms, messages, tools=schemas, dispatch=my_tools)  # async
```
`dispatch` maps each tool name to the function that runs it. (Want to drive the
loop yourself? `llms.chat(messages, tools=schemas)` hands you the raw
`.tool_calls`.)

### If you want a call history and a live admin view

A file pool is perfect to start with, and a file already keeps your config across
restarts. A database earns its place for two *independent* reasons — pick either or
both: a full, queryable **history of every call** (which LLM served it, latency,
tokens, quality — also what the Optimizer uses to tune itself faster) via
`telemetry=llmbroker.sqlite.Telemetry(...)`, or **managing the
pool at runtime** through an admin UI via `registry=llmbroker.sqlite.Registry(...)`,
instead of editing a file by hand. The example below uses both — the common
admin-UI case; for **call history only**, keep `registry=llmbroker.Registry("llms.toml")`
and add just `telemetry=`. Pointing the broker at a DB backend instead of a file
doesn't change your calling code. A DB backend holds a connection open, so close it with `with`:
```python
import llmbroker, llmbroker.sqlite

with llmbroker.Broker(
    registry=llmbroker.sqlite.Registry("broker.db"),
    telemetry=llmbroker.sqlite.Telemetry("broker.db"),
    seed=llmbroker.Registry("llms.toml"),   # bootstrap DB from file on first use
) as llms:
    ...   # now llms.calls(limit=...) and llms.snapshot() give you the admin view
```

### If you run several copies at once

Running more than one instance (say, behind a load balancer)? Give them a shared
store so they agree on which LLMs are cooling down, instead of each
re-discovering it the hard way. A single process never needs this:
```python
import llmbroker.redis
shared = llmbroker.redis.SharedState("redis://...")   # → Broker(..., shared_state=shared)
```

---

## The usage ladder (this is the README and the doc structure)

Documentation reads as a staircase, **not** as "orthogonal axes". Each rung is a
shipped battery; a reader stops at the first rung that fits.

**One battery rule, no exception list:** everything dependency-free is a
top-level class you construct directly with only `import llmbroker`
(`llmbroker.Registry(path)`, `llmbroker.Secrets()`, `llmbroker.Telemetry()`,
`llmbroker.JsonlTelemetry(path)`, …); a backend that carries an external
dependency is its own **submodule** you import explicitly — `import
llmbroker.sqlite` is *where* the optional dependency is pulled. Construct
submodule classes **fully qualified** (`llmbroker.sqlite.Registry(...)`);
**never** `from llmbroker import sqlite` (the bare `sqlite` shadows the reader's
stdlib mental model — an antipattern). The dividing line is just "does it have a
dependency", so there is no list of "which submodules are eager" to memorize.

### Rung 0 — install, pick a pool, one line (embedded, in-memory)

1. `pip install llmbroker`, then download a curated pool the project maintains
   (latest, independent of your package version — see "example files"):
   ```bash
   python -m llmbroker preset smart-freetier > llms.toml
   ```
2. Generate a `.env` skeleton so you never hand-type key names:
   ```bash
   python -m llmbroker env llms.toml > .env   # then fill in the values
   ```
3. In your app — one registry, no backend menu:
   ```python
   import llmbroker

   llms = llmbroker.Broker(registry=llmbroker.Registry("llms.toml"))   # sync — no await
   reply = llms.ask("Summarize this receipt: ...", operation="summary").text
   ```
   The synchronous `Broker` is the default most reach for. An async host (FastAPI,
   agents) uses `llmbroker.AsyncBroker` instead — identical surface, with `await`:
   ```python
   llms = llmbroker.AsyncBroker(registry=llmbroker.Registry("llms.toml"))
   reply = (await llms.ask("Summarize this receipt: ...", operation="summary")).text
   ```
   `llmbroker.Registry(path)` loads the config file and dispatches by extension
   (`.toml` / `.json`, both stdlib-parsed); an unknown extension is a clear error.

State is in-memory, telemetry goes to the log, keys come from env. Nothing to
implement, no secret in source. `ask` is the simplest call — it wraps a bare
string as one user message (`chat` is the full messages API). When every LLM is
momentarily busy it raises `NoLLMAvailable`; the README example shows handling
that. The broker starts its background machinery lazily on the first `ask`/`chat`,
so this one-liner needs no `start()` and no context manager (see "Lifecycle"); a
throwaway script on the default log telemetry can simply exit.

### Rung 1 — "if you have a database"

Persist config and telemetry, build an admin UI through the broker — or take just
one: `registry=llmbroker.sqlite.Registry(...)` alone gives DB-backed config + admin
CRUD (telemetry stays the default log); `telemetry=llmbroker.sqlite.Telemetry(...)`
alone gives a queryable call history while the pool stays in `llms.toml`. The
example below takes both — the common admin-UI case. Connecting to the store and
**populating** it happen together: pass `seed=` to the constructor and the pool is
seeded on first use (see "Seeding a DB store"). **`shared_state` is not part of
this** — a single process keeps cooldown state in memory internally; there is
nothing to share.

```python
import llmbroker
import llmbroker.sqlite          # dep-carrying → explicit import (llmbroker.Registry needs no import)

llms = llmbroker.Broker(
    registry=llmbroker.sqlite.Registry("broker.db"),
    telemetry=llmbroker.sqlite.Telemetry("broker.db"),
    seed=llmbroker.Registry("llms.toml"),              # bootstrap from file on first use
    # no shared_state= → single process (Rung 2 adds it for clusters)
)
```

`seed_policy=SeedPolicy.IF_EMPTY` (default) seeds once and steps aside — `add`/`remove`
through the admin API survive restarts. `SeedPolicy.ADD` only ever adds new entries.
`SeedPolicy.MIRROR` keeps the DB identical to the seed source every startup.

### Rung 2 — "if you run a cluster"

Add `shared_state=`; the instances then agree automatically (shared cooldown,
shared fail counts). Nothing else changes:

```python
import llmbroker.redis
shared_state=llmbroker.redis.SharedState("redis://...")   # or llmbroker.postgres.SharedState(dsn) / llmbroker.mongodb.SharedState(uri)
```

The broker core is **never cluster-aware** — clustering lives entirely inside the
`SharedState` implementation (see "Cluster coordination"). Omit `shared_state=`
and you are single-process; there is no "local" variant to write.

### Need an HTTP service?

`llmbroker` is a **library, not a server** — it deliberately ships no HTTP layer.
If you want a standalone gateway, embed the broker in whatever web framework you
already use (FastAPI / Flask / Django) and expose your own endpoint. That is a
host concern, outside the package's scope.

---

## Ports and the public surface (the universality contract)

Narrow Protocols. A host implements one **only** to support a backend we do not
ship.

```python
class LifecyclePhase(Enum):   # the catalogue of lifecycle phase codes
    AVAILABLE = "available"   # in rotation
    COOLING = "cooling"       # cooling after 429/503 until cooldown_until
    OFFLINE = "offline"       # repeatedly failed; sleeping before a probe (Optimizer, P4)
    PROBING = "probing"       # sending a test request to check recovery (Optimizer, P4)


# A snapshot of one LLM's live state, its field values fixed at the moment it is
# built — it is NOT stored and held. The broker builds a fresh one every time you
# `await llms[name].state()`, and builds one each time it saves to the shared store in a
# cluster. Plain fields only (no live properties), so it can be saved to and loaded
# from redis/postgres.
@dataclass(frozen=True, slots=True)
class LLMState:
    phase: LifecyclePhase = LifecyclePhase.AVAILABLE   # AVAILABLE/COOLING computed from cooldown_until vs now; OFFLINE/PROBING set by the Optimizer
    cooldown_until: datetime | None = None             # when the COOLING/OFFLINE sleep ends
    fail_count: int = 0


@dataclass(frozen=True, slots=True)
class LLMConfig:                         # pure stored config — no secret, safe to expose
    name: str                            # immutable identifier; every reference uses it; the Mapping key
    base_url: str
    model: str
    api_key_ref: str                     # env-var name / secret path; resolved via Secrets (broker-side)


class CallStatus(Enum):
    OK = "ok"                       # HTTP 200 — quality is judged separately via quality_score
    RATE_LIMITED = "rate_limited"   # 429
    UNAVAILABLE = "unavailable"     # 503
    ERROR = "error"                 # any other transport/protocol failure


@dataclass(frozen=True, slots=True)
class Usage:                             # resource use the provider reported for one call
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    extra: dict[str, int] | None = None  # provider-specific extras (cached / reasoning tokens, …)


@dataclass(frozen=True, slots=True)
class Call:
    id: str                              # broker-assigned uuid; PK of llmbroker_calls; the row record_quality updates
    llm_name: str                        # the LLMConfig.name that served this call
    operation: str | None
    trace_id: str | None
    status: CallStatus                   # coarse transport outcome — the axis routing reacts to
    http_status: int | None = None       # exact code (500/timeout → None); captured now, unrecoverable later
    latency_ms: int | None = None
    error_detail: str | None = None
    usage: Usage | None = None           # token counts the provider returned, when present
    quality_score: float | None = None   # 0..1; NULL = not judged (the common case)


@dataclass(frozen=True, slots=True)
class LLMMetrics:                        # per-LLM admin read-model, derived from Call rows
    call_count: int
    last_status: CallStatus | None
    last_at: datetime | None


@dataclass(frozen=True, slots=True)
class Alert:                             # one human-actionable signal from the Optimizer (P4)
    message: str                         # P1 placeholder shape — alerts() always returns [] until the
                                          # Optimizer exists; the real fields (kind/severity/llm_name/…)
                                          # are a P4 decision, same "not declared final" treatment as LLMState


# Port interfaces are named `<Capability><Port>Protocol`; a custom backend implements
# the level it supports. The bare names (Registry/Secrets/Telemetry) are the
# default concrete batteries. `Protocol` is the invariant suffix marking "this is a
# structural interface"; a capability is an adjective prefix (see "Naming convention").

# Minimal contract the broker needs — load the config. The file battery
# (llmbroker.Registry) implements exactly this.
class RegistryProtocol(Protocol):
    async def load(self) -> list[LLMConfig]: ...


# Admin extension the host admin UI types against (DB batteries implement it; the
# broker never calls it). A typed contract, not "optional methods" — a host admin
# function annotates `MutableRegistryProtocol` and gets full type checking on CRUD over
# ANY backend that supports it, with no concrete-type lock-in.
@runtime_checkable
class MutableRegistryProtocol(RegistryProtocol, Protocol):
    async def get(self, name: str) -> LLMConfig | None: ...
    async def add(self, cfg: LLMConfig) -> None: ...
    async def update(self, cfg: LLMConfig) -> None: ...   # keyed by cfg.name (immutable); fully typed, no **fields
    async def remove(self, name: str) -> None: ...


class SecretsProtocol(Protocol):
    async def resolve(self, ref: str) -> str: ...


# Admin extension for a writable secrets store (DB/vault/cloud batteries implement
# it; the broker's own resolution never calls it). Mirrors MutableRegistryProtocol:
# a typed contract a host admin function annotates against, with no concrete-type
# lock-in. The read-only batteries (Secrets(), DictSecrets()) do NOT implement this —
# calling .set() on them raises SecretsReadOnlyError.
@runtime_checkable
class MutableSecretsProtocol(SecretsProtocol, Protocol):
    async def set(self, ref: str, value: str) -> None: ...


# Optional, opt-in — only for clusters (several broker copies sharing one
# redis/postgres store so they agree on each LLM's state). A plain read/write store
# of the whole LLMState — the broker builds the value it writes at write time.
# Serialization is tolerant of LLMState evolution: read() ignores unknown fields and
# defaults missing ones, so a later release can add LLMState fields without breaking an
# already-deployed cluster backend (see the LLMState evolution note in "Ports").
class SharedStateProtocol(Protocol):
    async def read(self) -> dict[str, LLMState]: ...                # current state of every LLM in the store
    async def write(self, name: str, state: LLMState) -> None: ...  # save one LLM's whole state (phase included)


# Minimal contract — record a call, and attach a quality score to one already recorded.
# Both default log/none batteries implement exactly this. `record_quality` is on the
# minimal contract (not the queryable layer) so EVERY backend has a quality write path;
# how it lands differs by capability: a queryable backend UPDATEs the call row by id, an
# append-only backend appends a distinct, clearly-labelled quality record (never a Call).
class TelemetryProtocol(Protocol):
    async def record(self, call: Call) -> None: ...
    async def record_quality(self, call_id: str, score: float) -> None: ...


# Read/aggregation extension (queryable batteries — sqlite/jsonl/postgres — implement
# it; default log/none do not). The P1 shape is exactly what a host admin UI needs —
# no raw SQL. `@runtime_checkable` so the Optimizer can `isinstance` the telemetry to
# decide warm-start vs cold-boot — no hasattr sniffing. NB: the Optimizer's own
# warm-start aggregate shape (per-(llm, operation), quality/latency) is decided in P4
# and may add methods here — additively where possible, a synchronized breaking release
# otherwise; not pre-locked now (see "Autonomous optimization").
@runtime_checkable
class QueryableTelemetryProtocol(TelemetryProtocol, Protocol):
    async def metrics(self, *, since: datetime | None = None) -> dict[str, LLMMetrics]: ...  # per-LLM aggregates; broker builds snapshot() from this; default window when None
    async def calls(self, *, limit: int) -> list[Call]: ...
    async def purge_calls(self, *, before: datetime) -> int: ...  # retention — drop rows older than `before`


# Lifecycle capability shared by every port that holds an open resource (sqlite/redis/
# postgres/mongodb connections). It is ORTHOGONAL to a port's data contract — not inherited
# by RegistryProtocol/TelemetryProtocol/… — so a zero-resource port (file Registry, env
# Secrets, log Telemetry, NoTelemetry) simply does not implement it. `@runtime_checkable`
# so aclose() teardown is decided by `isinstance(port, AsyncResourceProtocol)`, never by
# hasattr sniffing — the same structural mechanism QueryableTelemetryProtocol uses. aclose()
# is idempotent (a second call is a no-op).
@runtime_checkable
class AsyncResourceProtocol(Protocol):
    async def aclose(self) -> None: ...
```

**The data types — who's who** (the README carries this same table):

| Type | Axis | Role |
|---|---|---|
| `LLMConfig` | config | a stored `(name, base_url, model, api_key_ref)` row; what `RegistryProtocol.load()` returns; no secret |
| `AsyncLLM` / `LLM` | facade | the `Mapping` value `llms[name]`; one handle bundling sync `.config` + `.state()` + `.metrics()` (async on `AsyncLLM`, sync on `LLM`) |
| `LifecyclePhase` | enum | the FSM label: Available / Cooling / Offline / Probing |
| `LLMState` | live | a snapshot of one LLM's runtime state `(phase, cooldown_until, fail_count)`, built on read; also what `SharedStateProtocol.read()`/`write()` stores in a cluster |
| `LLMSnapshot` | frozen report | a point-in-time materialization of one LLM `(config, state, metrics)`, all sync fields; the value type of `snapshot()` |
| `Usage` | event | token counts the provider reported for one call `(prompt_tokens, completion_tokens, total_tokens, extra)`; on `Result.usage` and `Call.usage` |
| `LLMMetrics` | aggregate | per-LLM `(call_count, last_status, last_at)` derived from `Call` rows; `QueryableTelemetryProtocol.metrics()` / `LLM.metrics()` |
| `Call` | event | one telemetry record (`id`, `llm_name`, `operation`, `status`, `usage`, `quality_score`, …); `id` is the uuid `record_quality` updates by |

### Two clients: `AsyncBroker` (the engine) and `Broker` (the shipped sync wrapper)

The async `AsyncBroker` is the core (its concurrency model — per-LLM queue slot, one
in-flight request, cooldown re-enqueue — is asyncio). The synchronous `Broker` is a
**first-class shipped wrapper** over an `AsyncBroker` running on a dedicated background
event-loop thread (see "Sync wrapper"); it is what the casual majority reaches for, async
hosts (dinary) use `AsyncBroker`. The split mirrors the SDKs users already know —
`Anthropic`/`AsyncAnthropic`, `openai.OpenAI`/`AsyncOpenAI`, `httpx.Client`/`AsyncClient`:
**clean name = sync, `Async` prefix = async.** The two surfaces are signature-identical
apart from `async`/`await` and `aclose`/`close`, `async with`/`with`.

```python
@dataclass(frozen=True, slots=True)
class LLMSnapshot:               # frozen point-in-time materialization (value type of snapshot())
    config: LLMConfig
    state: LLMState
    metrics: LLMMetrics | None    # None when telemetry is not queryable


class AsyncLLM:                  # handle returned by AsyncBroker[name] — config + a ref back to the broker
    config: LLMConfig            # sync — the pure stored config (name/base_url/model/api_key_ref); never lies
    async def state(self) -> LLMState: ...     # async — live (phase/cooldown_until/fail_count); reads shared
                                               #   state in a cluster, in-memory single-process; always the TRUTH
    async def metrics(self, *, since: datetime | None = None) -> LLMMetrics: ...  # this LLM's aggregate; default window when None
    # the resolved secret is NOT here and NOT on config — it lives in the broker's
    # private _resolved_keys map, keyed by name, and never leaves the broker.


class AsyncResult:                 # returned by AsyncBroker.ask()/chat()
    text: str                      # the assistant's reply content; "" when the reply is tool-calls-only
    tool_calls: list[dict] | None  # raw `tool_calls` from the response, verbatim; None when absent
    usage: Usage | None             # token counts the provider reported, when present
    async def record_quality(self, score: float) -> None: ...  # writes quality_score onto the Call
                                                                  # this Result was built from (matched
                                                                  # by the broker-assigned Call.id)


# `Result` is the synchronous analogue, returned by Broker.ask()/chat(): identical
# fields, `record_quality` blocks instead of awaiting — see "Sync wrapper".


class SeedPolicy(Enum):    # how the constructor seed reconciles the DB with the source
    MIRROR = "mirror"      # DB = source exactly: add new, update changed, remove absent
    ADD = "add"            # add absent entries only; never remove or update
    IF_EMPTY = "if_empty"  # seed only when registry is empty; no-op if already populated


class AsyncBroker(Mapping[str, AsyncLLM]):
    def __init__(
        self,
        *,
        registry: RegistryProtocol,                  # e.g. llmbroker.Registry("llms.toml") or llmbroker.sqlite.Registry(...)
        secrets: SecretsProtocol | None = None,      # default llmbroker.Secrets() — env; broker resolves api_key_ref → _resolved_keys
        shared_state: SharedStateProtocol | None = None,   # opt-in, cluster only
        telemetry: TelemetryProtocol | None = None,  # default llmbroker.Telemetry() — log
        optimize: bool | Optimizer = True,           # True ≡ Optimizer() (judge_fraction=0.0); see "Autonomous optimization"
        seed: RegistryProtocol | None = None,        # optional bootstrap source; applied on first ensure_pool()
        seed_policy: SeedPolicy = SeedPolicy.IF_EMPTY,
    ) -> None: ...                                   # cheap & side-effect-free; background loops start lazily

    async def aclose(self) -> None: ...
    async def __aenter__(self) -> "AsyncBroker": ...
    async def __aexit__(self, *exc: object) -> None: ...

    # ── primary role: route a completion across the pool. Always raise, never a sentinel.
    # `wait` bounds the capacity wait for a free LLM slot: None = wait indefinitely (default),
    # 0 = do not wait (raise NoLLMAvailable at once), N = wait up to N seconds then raise
    # NoLLMAvailable. AllLLMsFailed fires when a slot was obtained but the LLM(s) errored.
    # `wait` stays distinct from a future per-request provider timeout — capacity, not response.
    async def ask(self, prompt: str, *, operation: str | None = None,
                  trace_id: str | None = None, wait: float | None = None) -> AsyncResult: ...
    async def chat(self, messages: list[dict], *, tools: list[dict] | None = None,
                   operation: str | None = None,
                   trace_id: str | None = None, wait: float | None = None) -> AsyncResult: ...
    # `tools` is passed through verbatim (wire-format, like `messages` — see "Two entry
    # points"); NB beyond that, no per-call provider passthrough — see "Provider-specific
    # parameters" below

    # ── inspection: Mapping[str, AsyncLLM] over EVERY configured LLM (health shows in state().phase) ──
    def __getitem__(self, name: str) -> AsyncLLM: ...
    def __iter__(self) -> Iterator[str]: ...
    def __len__(self) -> int: ...

    # ── frozen report of the WHOLE pool in one round-trip — not a parallel live map, a value ──
    async def snapshot(self, *, since: datetime | None = None) -> Mapping[str, LLMSnapshot]: ...

    # ── change the pool (single items). Delegate to the mutable registry AND reconcile the live
    # pool (resolve key via Secrets, create/drain the queue slot). `add` upserts by cfg.name.
    # Require a mutable registry; a file Registry raises a clear "edit the file" error. ──
    async def add(self, cfg: LLMConfig) -> None: ...
    async def remove(self, name: str) -> None: ...

    # ── lazy idempotent pool initializer — applies constructor seed, then loads registry into pool.
    # Double-checked locking: safe for concurrent callers. __aenter__, chat, snapshot, add, remove
    # all call it automatically; call explicitly for eager fail-fast init at startup. ──
    async def ensure_pool(self) -> None: ...

    # ── call journal (require a queryable telemetry backend; else a clear error) ──
    async def calls(self, *, limit: int) -> list[Call]: ...
    async def purge_calls(self, *, before: datetime) -> int: ...

    # ── human-only signals (P4 Optimizer; empty list when optimize=False) ──
    async def alerts(self) -> list[Alert]: ...
```

The synchronous `Broker` (shipped, first-class) is the same surface without `async`/`await`,
with `close()`/`with` instead of `aclose()`/`async with`; it returns `LLM`/`Result` (sync
analogues of `AsyncLLM`/`AsyncResult`). `LLMSnapshot` is shared (it is plain data). See
"Sync wrapper".

```python
# async host (dinary)
llms = llmbroker.AsyncBroker(registry=llmbroker.sqlite.Registry("broker.db"))
cfg = llms["groq-llama"].config                              # sync, static
print(cfg.model, (await llms["groq-llama"].state()).phase)   # live, async
report = await llms.snapshot(since=midnight)                 # whole pool, one frozen value
for name, s in report.items():
    print(name, s.config.model, s.state.phase, s.metrics.call_count)

# casual / sync host — same code, no await, clean name
llms = llmbroker.Broker(registry=llmbroker.sqlite.Registry("broker.db"))
report = llms.snapshot()
for name, s in report.items():
    print(name, s.config.model, s.state.phase)
```

- **The schema is private; the broker is the public contract.** No host issues raw
  SQL against `llmbroker_registry`/`llmbroker_calls`, and **no host calls a port method
  directly** — config CRUD goes through `llms.add`/`remove`, live state
  through `await llms[name].state()` (or the whole-pool `await llms.snapshot()`), and
  call-log read/retention through `await llms.calls()`/`purge_calls()`. The ports
  (`MutableRegistryProtocol`, `QueryableTelemetryProtocol`) are contracts the **broker**
  consumes and **backend authors** implement, not surfaces the host touches. This is what
  lets the package own and evolve its schema independently after extraction; a host admin
  UI is built entirely on broker methods and works identically over any backend
  (sqlite/postgres/mongodb), which a fixed table shape never could.
- **The broker is a read-only `Mapping`.** Indexing it is one level (`llms[name]`,
  never `llms.llms[name]`); the handle (`AsyncLLM`/`LLM`) bundles sync `.config` (the
  cached `LLMConfig`) + `.state()` (live `LLMState`, the truth) + `.metrics()` — no sync
  field that could go stale. The same `LLMState` value is what `SharedState.read()`/`.write()`
  stores in a cluster. The Mapping spans **every** configured LLM; health is read via
  `state().phase`, so there is no separate "configured vs managed" view to reconcile.
  The whole-pool view is a separate, frozen value — `await llms.snapshot()` — not a
  parallel live map.
  **Why the broker *is* the Mapping (not a `.pool`/`.llms` sub-attribute).** The host's
  variable is `llms` regardless of the class name, so both roles read naturally under it:
  `llms.chat(...)` (the call) and `llms[name]`, `name in llms`, `len(llms)` (inspection).
  A `.pool` sub-attribute buys nothing over direct indexing, and `.llms` would force the
  `llms.llms[name]` stutter. **A `Mapping` that also performs I/O is admittedly unusual**
  (most mappings are passive); it is justified because there is genuinely **one** object
  with one host variable, and both readings of it are honest. Splitting them would only
  manufacture the `llms.pool[name]` stutter the single-object design exists to avoid.
- **One rule governs sync vs async on `AsyncBroker`/`AsyncLLM`: a member is `async` iff
  it performs I/O.** In-memory / cached access is sync — `llms[name]`, `.config` (the
  static stored config, which cannot lie), `Result.text`/`.usage`, `name in llms`,
  `len(llms)`. Anything touching the network, a file, or the DB is async — `ask`/`chat`,
  the config mutators (`add`/`remove`/`ensure_pool`), `AsyncLLM.state()`/`.metrics()`,
  `snapshot()`, `calls`/`purge_calls`, and `Result.record_quality()`. **`state()` is
  deliberately async, not a sync property:** in a cluster the truth lives in the shared
  store, so reading it is I/O — a sync field would silently show this instance's stale
  mirror. The static `config` is the only live-ish member that stays sync, because it is
  the value you set, not a volatile signal. The synchronous `Broker` wrapper blocks
  instead of awaiting (see "Sync wrapper") — same surface, no `await`.
- **Mutation is a broker method, not a `MutableMapping`.** The broker stays a
  **read-only** `Mapping` — you cannot `llms[new_key] = …` (assigning a handle by key has
  no construction semantics; the key lives inside `cfg.name`; and the op is I/O, so it
  can't be a sync `__setitem__`). Config changes go through the named methods
  `llms.add(cfg)` (upsert by `cfg.name`) / `llms.remove(name)` (single items).
  Each delegates to the mutable registry backend **and** reconciles the live pool in the
  same call (resolve the key via `Secrets`, create or drain the queue slot), so `llms`
  reflects the change atomically — no user-facing refresh, no "configured but not yet
  live" gap. A host **never** calls `registry.add()` itself; the registry port's CRUD is
  what the broker delegates to and what the package's own CLI uses for offline seeding. On
  an immutable file `Registry` (it implements only `RegistryProtocol`) these raise a clear
  "edit the file" error.
- **Layered protocols, not "optional methods".** A custom backend implements the
  level it supports, and each level is a real, type-checked contract. `RegistryProtocol`
  (just `load()`) is all the broker needs to *read* config; `MutableRegistryProtocol(RegistryProtocol)`
  adds `get`/`add`/`update`/`remove` — the **broker** requires it to offer
  `llms.add`/`remove` and apply the constructor seed, and `isinstance`-checks the registry
  so a file backend gives a clear error instead of a missing method. Likewise `TelemetryProtocol` (just
  `record()`) vs `QueryableTelemetryProtocol(TelemetryProtocol)` (`metrics`/`calls`/`purge_calls`)
  for the call-log read side; the default `Telemetry()` (log) / `NoTelemetry()` implement
  only `TelemetryProtocol`, so `llms.calls()`/`purge_calls()` raise a clear error on them
  (and `snapshot()`'s `metrics` field is `None`). This mirrors `Sequence`/`MutableSequence`: the broker annotates the capability it
  requires rather than sniffing `hasattr`, and a host that swaps sqlite→postgres changes
  nothing. The richer protocols are `@runtime_checkable` so both the broker (capability
  gating) and the Optimizer (`isinstance(telemetry, QueryableTelemetryProtocol)` for
  warm-start vs cold-boot) decide by `isinstance`, never `hasattr`.
- `LLMMetrics` is a small per-LLM read-model for admin aggregates (`call_count`,
  `last_status`, `last_at`) — derived from `Call` rows, never a stored table of its own.
  The per-name `await llms[name].metrics()` returns one (a query scoped to that LLM); the
  whole-pool view is `await llms.snapshot()`, which bundles each LLM's `config`+`state`+`metrics`
  into a frozen `LLMSnapshot` in **one** round-trip (one `SharedState.read()` + one
  telemetry aggregate). `metrics` is named for the telemetry-domain standard
  (OpenTelemetry's *metrics* signal), cleanly distinct from `state()`.
- `snapshot()` is a **frozen materialization**, not a second live map: `await llms.snapshot()`
  returns a plain `Mapping[str, LLMSnapshot]` you index/iterate like any dict, captured at
  one instant. It is `snapshot()` (cf. `tracemalloc.take_snapshot()`), **not** `copy()`
  (which must preserve the value type and be a sync shallow copy) — it changes the value
  type to the resolved `LLMSnapshot` and does I/O. This is what removes the "two parallel
  dicts" smell: `llms` is the live handle-Mapping, `snapshot()` is a point-in-time report value.
- `SharedState` is **optional and cluster-only** — omit `shared_state=` and the
  broker uses its private in-memory state. There is no public "in-memory
  SharedState" object; single-process is the absence of the parameter.
- `SharedState` is a plain read/write store of the whole `LLMState`: `write(name,
  state)` saves one LLM's state, `read()` returns every LLM's current state. The
  broker builds the `LLMState` value at the moment it writes (e.g. on a 429 it
  computes the new state and saves it). Writing the **whole** state — not granular
  events — is what lets every phase, including the Optimizer's Offline/Probing,
  propagate to other copies with no extra method.
- `LLMState.phase` carries the **full** `LifecyclePhase` enum
  (Available/Cooling/Offline/Probing) from day one — the labels are knowable without
  the Optimizer, so fixing them now is cheap and harmless. P1 only ever sets
  Available/Cooling (429/503 cooldown); Offline/Probing are populated by the
  Optimizer (P4). **`LLMState` is not declared final, though.** The Optimizer (P4) will
  likely add tuning fields that must also sync across a cluster — `current_delay`, the
  offline-sleep length, a probe counter — none of which `phase`/`cooldown_until` encode
  (a cluster peer reading shared state today inherits the *phase* and next-wake time, but
  not the escalation level driving the next transition). So the contract here is **not
  "the shape is frozen" but "SharedState serialization tolerates evolution"**:
  `read()` ignores unknown fields and defaults missing ones, so P4 can add `LLMState`
  fields without breaking an already-deployed P3 backend. Where a change cannot be made
  additively, it is a synchronized breaking release of `llmbroker`+dinary (dinary pins a
  version) — an ordinary dependency bump, not something P1 must contort to avoid.
- **`phase` is always derived for `Available`/`Cooling`, never trusted as stored.**
  Every time the broker builds an `LLMState` — for `state()`, `snapshot()`, or after
  `SharedState.read()` returns a peer's value — it recomputes `phase` as `COOLING`
  iff `cooldown_until` is set and still in the future, `AVAILABLE` otherwise. A peer's
  stale `COOLING` whose `cooldown_until` has since passed therefore never leaks into
  `state().phase`; reading shared state needs no extra reconciliation step. Only
  `OFFLINE`/`PROBING` (P4) are trusted as stored — they have no `cooldown_until`-based
  derivation.
- `Call` captures token `usage` (objective, read from the response, when present)
  and `quality_score` from P1 because telemetry is
  **append-only** — a column added later starts with no history, which is exactly
  the data the Optimizer needs. `quality_score` is **orthogonal to `status`**:
  `status` is the transport outcome (an HTTP-200 answer is `status=CallStatus.OK`),
  `quality_score` is whether that answer was usable. **Cost is deliberately not
  stored** — it is `tokens × a price table`, a host/Optimizer concern derived later
  from the tokens, not a raw signal to journal. The **source** of `quality_score`
  (host `score()` vs the P5 LLM-judge) is **not** a separate column in P1: until
  the judge exists every score is a host `score()` ground truth, so pre-judge rows
  are unambiguous and a `quality_source` column can be added with the judge (P5)
  with no lost history.
- `registry=` takes a `RegistryProtocol`; build one with `llmbroker.Registry(path)`
  (file) or `llmbroker.sqlite.Registry(...)` — **the port is only a backend selector you
  construct**, every operation on it is reached through the broker. Programmatic config
  goes through `llms.add`/`remove`; bootstrap from a source via the constructor `seed=`
  parameter (a `RegistryProtocol`, e.g. `llmbroker.Registry("llms.toml")`). The kwarg
  matches the port, like `secrets=`/`shared_state=`/`telemetry=` taking a
  `SecretsProtocol`/`SharedStateProtocol`/`TelemetryProtocol`.
- **Two entry points, each with one clean type — no polymorphic parameter.**
  `chat` is the full API and always takes a chat messages array; `ask` is a thin
  convenience for the dominant single-user-turn case. Both return a `Result`
  handle exposing `.text`, `.usage`, and `.record_quality(...)`. Rung 0 is
  `llms.ask("Summarize …")`; anything beyond one user turn (system prompt,
  multi-turn history, assistant context) goes through `chat(messages)`. Keeping
  `messages` a single honest type avoids the `str | list` chameleon — the
  convenience lives in a separate, unambiguous method, not in an overloaded arg.
  There is **no per-call provider passthrough** — the broker does not know which
  provider will serve a call, so raw provider body fields have no place in its API
  (see "Provider-specific parameters").
- **`chat` accepts an optional `tools: list[dict] | None = None`, passed through
  verbatim alongside `messages`.** Unlike a provider tuning knob (`temperature`,
  `response_format`), the `tools`/`tool_calls` JSON-schema shape is part of the same
  OpenAI-compatible chat-completions wire contract `messages` already is — every
  provider this broker targets accepts the same `tools` array and returns
  `tool_calls` the same way, so passing it through is the same "honest pass-through
  of provider wire format" as `messages`, **not** the rejected per-call
  provider-params passthrough (see "Provider-specific parameters"). `Result` gains
  `.tool_calls` (the raw `tool_calls` list from the response, `None` when absent)
  alongside `.text`/`.usage`/`.record_quality(...)`. `ask` takes no `tools=` — tool
  use implies a multi-turn loop, which is `chat`'s domain. The broker itself runs no
  tool-call loop: one call is one routed request, returning whatever the chosen LLM
  answered, tool calls included. Orchestrating "execute the tool, append the result,
  call again" is the host's job — `llmbroker.run_tool_loop` / `llmbroker.arun_tool_loop`
  (see "Shipped batteries" / package layout) are host-agnostic helpers for exactly
  that, built on top of `chat(messages, tools=..., ...)`.
- **One pair of methods, a numeric `wait`, always raising — no `try_*` twins, no
  `wait` *flag*.** There is no honest "blocking vs non-blocking" split to make: even
  the so-called blocking call goes `await` and waits on the chosen LLM, which can
  itself stall and end in a timeout or error, so a second method buys no different
  contract. The only real question is *what to do while no LLM slot is free*, and that
  is a duration, not a mode: `wait: float | None` — `None` waits indefinitely (default),
  `0` does not wait, `N` waits up to N seconds — after which the call **raises**
  `NoLLMAvailable`. This is exactly the `lock.acquire(timeout=)` / `queue.get(timeout=)`
  idiom: a numeric bound that **raises** on expiry, so the return type never shifts.
  Note this is **not** the rejected boolean `wait=`: that flag was bad because it would
  flip the *return contract* (raise vs sentinel) — a numeric `wait` that always raises
  keeps one contract and never returns `None`. Best-effort, skippable work ("enrich if a
  slot is spare, else move on") is `chat(..., wait=0)` inside `try/except
  NoLLMAvailable` — one obvious branch, no second method to learn.
  **The `wait=None` (wait indefinitely) default suits the broker's core job — ride out
  finite 429/503 cooldowns rather than push retry onto every caller — and is what a
  queue worker (dinary's classifier) wants.** An **interactive** caller that must bound
  latency should pass a finite `wait`; the README leads with this so the default is never
  a surprise hang under sustained backpressure.
- Both `ask` and `chat` take an opaque `trace_id` (correlation) and an
  `operation: str | None` (a host-defined category — e.g. `"receipt_classification"`,
  `"summary"`). `operation` is what lets the `Optimizer` tune and route per
  operation, so it is captured from day one even though the Optimizer is built
  later. **The word `operation` is deliberate and collision-free**: HTTP's term for a
  request kind is "method" (and Python's is "method" too), so `operation` does not clash
  with either — it is an unclaimed, immediately legible name for "the kind of work this
  call is", exactly the host-defined routing/tuning axis the Optimizer keys on.
- **`ask`/`chat` raise rather than returning a sentinel.** An `LLMRequestError`
  hierarchy — `NoLLMAvailable` (no LLM slot came free within `wait`) and
  `AllLLMsFailed` (a slot was obtained but each tried LLM errored) — replaces a
  `str | None` return, so "no capacity" is never confused with an empty answer.
  `NoLLMAvailable` means "`wait` elapsed and the pool is still busy" — with `wait=0`
  that is immediate, with `wait=None` it never fires (the call waits out cooldowns).
  `AllLLMsFailed` is orthogonal: it fires whenever an LLM was actually tried and
  errored, regardless of `wait`, because that is a real failure, not a capacity skip.
  **In practice `LLMRequestError` is the catch most callers reach for** — "this
  request could not be completed" — since from a caller's perspective "no slot was
  free" and "every LLM tried failed" are usually the same outcome (give up / fall
  back / retry later). The two subclasses exist for the rarer caller that reacts
  differently to a capacity skip vs. a real failure; everyone else catches the
  common base.

`Result.record_quality(score: float)` — `async`, since it writes to telemetry; the
verb is honest about the side effect, parallel to `TelemetryProtocol.record(call)`, and
avoids "rate" colliding with rate-limiting. It does **not** emit a second `Call`.
Quality attaches to the **existing** call: every `Call` carries a broker-assigned
`id` (a uuid set at call time, the primary key in `llmbroker_calls`), and the `Result`
holds that id, so the quality score is routed to the original row. **The id is a uuid,
not a DB sequence/autoincrement, on purpose:** it must exist the instant the broker
creates the `Call` (so it can ride the in-memory `Result` for a later
`record_quality`) — a sequence is assigned only at `INSERT`, forcing a `RETURNING`/
`lastrowid` round-trip and back-threading — and it must mean the same thing across
**every** telemetry backend, including ones with no sequence at all (the log battery's
`quality call=<id>` line, jsonl, mongo) and a clustered multi-writer postgres where
broker-side uuids never collide and need no central id authority. The 16-byte / index-
locality cost is negligible for a retention-`purge`d event table; UUIDv7 is a drop-in if
ordering ever matters. `record_quality`
records the score into the broker's live state (mirrored to shared state if present)
and then calls `telemetry.record_quality(call_id, score)` — a method on
`TelemetryProtocol` whose two implementations diverge by what the backend can do:

- **Queryable backends** (`sqlite`/`jsonl`/`postgres`) `UPDATE llmbroker_calls SET
  quality_score=? WHERE id=?` — the score lands **on the original call row**. No new
  row, so `call_count`/aggregates never double-count.
- **Append-only backends** (`Telemetry()` log / `NoTelemetry()`), which cannot update a
  past line, append a **distinct, clearly-labelled quality record** (`quality call=<id>
  score=<v>`) — explicitly *not* a `Call` clone, and never tallied as a call.

A host marks an unusable answer with `record_quality(0.0)`; the P5 LLM-judge reuses the
**same** method to fill sampled non-binary scores, so there is one write path into
`quality_score`. `Call` carries `operation` alongside `trace_id` and `id`, so quality,
tokens, and latency are all attributed per (llm, operation) against one canonical row.

---

## Lifecycle — construct cheap, start lazily, close explicitly

The broker owns background machinery (per-LLM `asyncio.Queue` slots, the cooldown
re-enqueue timers, the P4 `Optimizer` loop) and, through its ports, open resources
(sqlite/redis connections). The lifecycle keeps the Rung-0 one-liner trivial while making
clean shutdown unambiguous.

- **`Broker(...)` is cheap and side-effect-free.** The constructor only stores
  config and ports — no loop work, no connections, no background tasks. It is safe
  to construct outside a running event loop.
- **Background loops and port connections start lazily on the first `await
  ask`/`chat`.** So Rung 0 needs no `start()` and no `async with`.
- **Teardown is `await llms.aclose()`** — it does two things: (1) cancels the
  broker's background loops (always — a running event loop holds strong refs to those
  tasks, so they are never GC-collected on their own and the task closures keep the
  broker alive), and (2) closes the **resource-holding** ports it owns. A resource-holding
  backend implements the `@runtime_checkable` `AsyncResourceProtocol` (a single
  `async def aclose(self)`); the broker decides what to close with
  `isinstance(port, AsyncResourceProtocol)` and calls `aclose()` only on the ports that
  match — the same structural-protocol mechanism used for `QueryableTelemetryProtocol`,
  **never** `hasattr` sniffing. A zero-resource port simply does not implement the protocol
  and is skipped, so it need not declare a no-op. In P1
  the only resource-holding port is
  `llmbroker.sqlite.*` (the aiosqlite worker thread + connection + the DB file fd, none
  of which GC reclaims promptly); P3 adds the redis/postgres/mongodb sockets. The
  zero-resource ports — file `Registry`, `Secrets`/`DictSecrets`, log `Telemetry`,
  `NoTelemetry` — do not implement `AsyncResourceProtocol` at all, so the broker skips them
  and a TOML+log broker's teardown is *only* the task cancellation.
- **Ports are owned by exactly one broker; resource ports are not shared.** The broker
  owns and closes every port handed to it. A resource port (`sqlite`/`redis`/`postgres`)
  belongs to one broker — if two brokers must talk to the same DB, each is given its own
  port on the same path/URL (sqlite allows several connections to one file; redis several
  pools to one server). This is not enforced in code (a port is just an object you could
  pass twice) and does not need to be: the constructor takes a path/URL, not a live
  connector, so the obvious wiring already gives each broker its own; and sharing a
  resource port is self-evidently wrong — whichever broker shuts down first would close
  the connection out from under the other (a *premature*-close bug, which no ownership
  trick fixes). Zero-resource ports (`Secrets`, log `Telemetry`) may be shared freely —
  they implement no `aclose()`. As cheap hygiene, every resource port's `aclose()` is
  **idempotent** (a second call is a no-op, never an error).
- **`async with` is teardown sugar over `aclose()`**, not a second way to start.
  `__aenter__` returns `self`. Because the constructor is multi-line, the idiom is
  **two-step** — never the constructor in the `with` header:

  ```python
  llms = llmbroker.AsyncBroker(registry=..., telemetry=...)
  async with llms:        # `as` is redundant; teardown guaranteed on exit
      ...
  ```

  The synchronous `Broker` mirrors this with `close()` and a plain `with llms:` (it owns
  the background event-loop thread and shuts it down on close).

- **Three levels, matched to the consumer:**
  1. **Throwaway script on the default log telemetry** — no teardown needed; the
     one-liner runs and the process exits (nothing buffered, no connection to flush).
  2. **Script/test with a DB or network battery** — use `async with llms:` for
     deterministic cleanup (flush the last telemetry writes, close connections, stop
     tasks leaking between tests).
  3. **Long-lived app (dinary/FastAPI)** — construct once, `await llms.aclose()` on
     shutdown (FastAPI lifespan); see "dinary wiring".

  The rule of thumb: **the moment a DB/network battery is attached, or the process
  does not immediately exit, close the broker.**

---

## Sync wrapper — the shipped synchronous `Broker`

`Broker` is a **first-class, shipped** synchronous client — the one the casual majority
reaches for (scripts, notebooks, sync web frameworks, CLIs, `inv` tasks). `AsyncBroker`
is the async engine; `Broker` wraps one running on a **dedicated background event-loop
thread**, and its blocking `ask()`/`chat()`/… submit the coroutine to that loop and wait
on the result. The pool's concurrency therefore persists across calls (unlike a per-call
`asyncio.run`, which would tear down and rebuild the pool every time). This is the
established two-client pattern — `Anthropic`/`AsyncAnthropic`, `openai.OpenAI`/`AsyncOpenAI`,
`httpx.Client`/`AsyncClient` — with the **clean name on the sync client**.

- **Same surface, no `async`/`await`.** Every method mirrors `AsyncBroker` one-for-one;
  `Broker` returns `LLM`/`Result` (sync analogues of `AsyncLLM`/`AsyncResult`) whose
  `state()`/`metrics()`/`record_quality()` block instead of awaiting. The shared frozen
  `LLMSnapshot` is identical (it is plain data). Teardown is `close()` / `with llms:`
  (vs `aclose()` / `async with`).
- **Thin proxies, one core.** The wrapper is a small layer of blocking proxies; all logic
  — the queue, cooldowns, demand-driven shared-state sync, the Optimizer — lives once in
  `AsyncBroker`. **unasync-style codegen is rejected**: the asyncio concurrency core cannot
  be produced by stripping `await`, so `Broker` delegates to a live `AsyncBroker` rather
  than being a generated sync copy.
- **Pick by host.** Async hosts (dinary/FastAPI, agents) use `AsyncBroker`; everything
  else uses `Broker`. Both ship in P1.

---

## Secrets — universal, trivial for the simple case

Stored config holds an `api_key_ref`, not a secret. The `Secrets` resolver turns a
ref into the actual key. Default reads env vars, so the simplest case is just "set
env vars".

```toml
# llms.toml
[[llms]]
name        = "groq-llama"       # immutable identifier; referenced everywhere; the Mapping key
base_url    = "..."
model       = "..."
api_key_ref = "GROQ_API_KEY"     # env-var name for llmbroker.Secrets() (env); a secret path for a vault resolver
```

```python
llmbroker.Broker(registry=llmbroker.Registry("llms.toml"))                          # default llmbroker.Secrets() (env): from os.environ
llmbroker.Broker(registry=..., secrets=llmbroker.DictSecrets({...}))                # explicit map (tests / pre-loaded keys)
llmbroker.Broker(registry=..., secrets=my_vault_resolver)                           # secret manager: implements .resolve(ref)
```

- **Resolution is a `Broker` concern, not a `Registry` one.** The registry returns
  **pure** `LLMConfig` (with `api_key_ref`, no secret); the broker calls
  `secrets.resolve(api_key_ref)` for each entry when it loads config and keeps the
  resolved keys in its **private** `_resolved_keys` map (keyed by `name`). So the
  resolved secret never rides a public object — `secrets=` lives on the `Broker`
  only, never on the registry constructor.
- Shipped: `llmbroker.Secrets()` (env, the default), `llmbroker.DictSecrets(mapping)`
  — both zero-dependency, so they are top-level classes, not a backend submodule. A
  plain `Callable[[str], Awaitable[str]] | Callable[[str], str]` is accepted and
  adapted, so a secret-manager integration is one small function.
- Keys are resolved when config is (re)loaded — on `llms.add` and pool init
  (`ensure_pool`); rotated secrets are picked up on the next load.

### Admin-editable secrets

A host building an admin UI that lets a user type in a provider's API key needs a
**writable** secrets store — the default env-backed `Secrets()` is read-only by
design (a running process cannot write its own environment back to disk).

- `MutableSecretsProtocol` extends `SecretsProtocol` with `set(ref, value)` (see
  "Ports"). The read-only batteries (`Secrets()`, `DictSecrets()`) do not implement
  it; calling `.set()` on them raises `SecretsReadOnlyError` with a clear message.
- Shipped mutable implementations: `llmbroker.sqlite.Secrets("broker.db")` (P1,
  no extra dependency beyond `aiosqlite`), `llmbroker.aws.Secrets(...)` (AWS
  Secrets Manager, P3), `llmbroker.vault.Secrets(...)` (HashiCorp Vault KV, P3) —
  the latter two each behind their own optional dependency extra (`llmbroker[aws]`,
  `llmbroker[vault]`).
- **Choosing a secrets backend is explicit and orthogonal to the registry choice.**
  The default (no `secrets=` argument) is env, read-only — fine with either
  `llmbroker.Registry` or `llmbroker.sqlite.Registry`. A host wanting an admin UI
  that edits keys picks a mutable backend explicitly:

  ```python
  llmbroker.Broker(
      registry=llmbroker.sqlite.Registry("broker.db"),
      secrets=llmbroker.sqlite.Secrets("broker.db"),
  )
  ```

  Plain examples elsewhere in this doc omit `secrets=` entirely — it is the
  default, and showing it would suggest it is required.

### Curated pools + the `env` command (so key names are never hand-typed)

- **Curated pool lists live in the repo source, not in the wheel.** Keep them as
  plain files in the project tree (`presets/freetier.toml`,
  `presets/smart-freetier.toml`, …; identical format, different LLM list) and have
  `python -m llmbroker preset <name> > llms.toml` **fetch the latest copy from the
  default branch** (the raw `presets/<name>.toml` URL). The reasoning: the LLM
  landscape (endpoints, free tiers) churns far faster than the broker code, so a
  list bundled into the package would be frozen at the installed version and go
  stale. Keeping the lists in source and fetching the branch tip means **updating a
  pool is just a commit to `presets/` — no PyPI release, no GitHub Release** — and
  every already-installed `llmbroker`, whatever its version, picks the new list up
  on the next `preset`. A pinned `preset <name> --ref <tag>` for reproducibility can
  be added later; the default is "latest".
- **Network is needed only for `preset`.** Offline, a user hand-writes `llms.toml`
  (the format is trivial) and everything else works; a failed fetch raises a clear
  error naming the URL it could not reach.
- Ship `python -m llmbroker env <toml> > .env`: scans any TOML (a fetched preset or
  a hand-written file) for `api_key_ref` and emits a `.env` skeleton with the key
  names and blank values — the single robust source, so there is no separate static
  `.env.example` to drift out of sync with the presets.

---

## Seeding a DB store — constructor `seed`, lazy on first use

Constructing a backend registry only **selects/connects** it; it starts empty. Pass
`seed` and `seed_policy` to the constructor; the broker applies the seed exactly
once on the first `ensure_pool()` call (triggered lazily by `chat`, `snapshot`,
`add`, `remove`, and `__aenter__`, or explicitly for eager fail-fast startup):

```python
llms = llmbroker.AsyncBroker(
    registry=llmbroker.sqlite.Registry("broker.db"),
    secrets=llmbroker.sqlite.Secrets("broker.db"),
    seed=llmbroker.Registry("llms.toml"),
    seed_policy=llmbroker.SeedPolicy.ADD,
)
await llms.ensure_pool()   # eager init at startup (fail-fast)
```

`seed_policy` is a `SeedPolicy` enum. It chooses **who is authoritative**, which in
turn decides whether `add`/`remove` are meaningful:

| `SeedPolicy` | On `ensure_pool` | Authoritative | `add`/`remove`? |
|---|---|---|---|
| `MIRROR` | DB = source exactly: add new, update changed, **remove** absent | The seed source | Don't — undone next boot |
| `IF_EMPTY` (default) | Seed only when registry is empty; no-op if already populated | The DB after first fill | Yes — they persist |
| `ADD` | Add absent entries only; never remove or update existing | Mixed | Partial — re-adds a removed name |

The CLI offers the same reconciliation offline (without a running application):

```bash
python -m llmbroker sync llms.toml --into sqlite:broker.db --policy mirror
```

Seeding requires a mutable registry (`MutableRegistryProtocol`); using a file
`llmbroker.Registry` as the main registry raises a clear "edit the file" error.

### Seeding secrets alongside configs

When applying the seed, the broker also bootstraps secrets — **fill gap,
don't overwrite** — for each synced `LLMConfig`:

- if `secrets.resolve(api_key_ref)` already succeeds, leave it as-is — preserves
  an admin's edited key or a pre-populated secrets store;
- else, try `llmbroker.Secrets()` (env vars). If found **and** `secrets` implements
  `MutableSecretsProtocol`, call `secrets.set(api_key_ref, value)` to persist it;
- else — no special handling. The broker's own resolution raises its usual clear
  error for that `api_key_ref` when the LLM is first used.

This is a one-time bootstrap transfer (env → secrets store), not a runtime
fallback: once seeded, the broker consults only its configured `secrets=`, never
env vars as a secondary source.

**`IF_EMPTY` on restart with a non-empty registry exits early without attempting
secret seeding** — by design: if the registry is already populated, secrets were
bootstrapped during the initial seed. `ADD` and `MIRROR` always attempt to fill
missing secrets on every `ensure_pool`, so a secret absent on first boot (e.g.
env var added later) is picked up on the next restart.

---

## Cluster coordination — how `SharedState` meets the in-memory queue

The broker keeps its single-process machinery: one `asyncio.Queue` slot per LLM,
at most one in-flight request per LLM, `loop.call_later` re-enqueue after a 429
cooldown, and its **private in-memory** per-LLM live state. `SharedState`, when
supplied, layers on **demand-driven** — synced when it matters, never on a timer:

- **Freshness is needed only at selection.** Reading shared state is **lazy at the
  moment the broker picks an LLM for a call**, with a short TTL to coalesce a burst of
  calls into one `read()`. An idle process (a user files a few receipts a day) makes
  **zero** calls to the shared store; cost scales with traffic, not wall-clock. A redis
  `read()` (~1 ms) is noise next to the LLM request (seconds) it precedes.
- **On 429/503 (write-through):** the broker updates its in-memory cooldown, schedules
  its own local `call_later` re-enqueue, and — if `shared_state=` is set — builds the new
  `LLMState` and calls `write(name, state)` so *other* copies learn. Writes happen only on
  a real state change, the only meaningful moment.
- **Cooldown expiry needs no polling.** A peer that read `cooldown_until` just compares it
  to `now()` locally; expiry is computed, not detected, so there is nothing to poll for.
- **No `shared_state=` (default):** everything stays in the process's own memory —
  identical to single-process behavior (local `call_later`), zero infra, zero races, and
  `state()`/`metrics()` simply read the in-memory mirror.
- **Shared backends** (`llmbroker.redis`/`postgres`/`mongodb` `.SharedState(...)`)
  exist **only for clusters**: `read()` returns the whole shared state in one round-trip;
  bounded races (two copies briefly both see an LLM free) cost at most one redundant 429.
  There is no `sqlite` `SharedState` — SQLite is not a cross-node store, and single-process
  needs no externalized state.

Granularity = the selection moment (eventual consistency only between calls, which is the
only window that exists). redis pub/sub for push propagation is noted as a **future
optimization**, not built now. **There is no user-facing `refresh()` and no background
poll** — drift is reconciled lazily at selection and eagerly on write, both automatic.

---

## Autonomous optimization — the `Optimizer`

Showing per-LLM advice is not the goal — **nobody will study what is happening
with yet another free LLM, or care which vendor backs it.** The goal is that the
cluster **tunes itself and routes work optimally, invisibly.** The package ships
an `Optimizer`: a background control loop that reads
telemetry and *acts*, not just reports.

```python
llms = llmbroker.Broker(
    registry=llmbroker.sqlite.Registry("broker.db"),
    telemetry=llmbroker.sqlite.Telemetry("broker.db"),   # queryable → warm-start + analysis (optimizer runs on any backend)
    optimize=True,                                        # default-on; learns from the live event stream
)
```

**The control surface — one knob for the AI part.** `optimize` takes `bool | Optimizer`:

```python
optimize=True                              # default: delay tuning + routing, ZERO extra LLM calls (active from P4)
optimize=False                             # broker stays reactive (round-robin + 429/503 cooldown), no learning
optimize=llmbroker.Optimizer(judge_fraction=0.05)   # the above + LLM-as-judge scores 5% of answers (active from P5)
```

`Optimizer(judge_fraction: float = 0.0)` — `judge_fraction` is the **sampling fraction** the
LLM-as-judge scores (`0.0` = off). `True` ≡ `Optimizer()` (`judge_fraction=0.0`), `False`
≡ no optimizer. So the default self-tuning is **free** (no extra LLM traffic), and
token-spending judging is **never** enabled implicitly — only when a host sets
`judge_fraction>0`.

**P1 ships only the shape, not the behavior.** P1 fixes the parameter
(`optimize: bool | Optimizer = True`, `Optimizer(judge_fraction=0.0)`) so the default is
locked now and P4 can switch the engine on with **no API change**. In P1 the
Optimizer loop does not exist: `optimize=True` runs nothing, and the broker is
**reactive regardless** — round-robin selection + per-LLM 429/503 cooldown. The
delay tuning + routing land in P4, the judge in P5. So `optimize=True` in P1 is an
honest reservation, not a working feature; do not document it as one.

**Why `bool | Optimizer`, not `Optimizer | None`** (so this is not re-litigated).
`bool | Optimizer` is a precise, fully type-checked union (not `Any`) and the
oldest of ergonomic Python idioms for a config knob — `True` = sensible default,
`False` = off, an object = custom (cf. `stdout=PIPE | None`, `retry: bool |
RetryConfig`). It is **not** the `str | list` polymorphism rejected for
`ask`/`chat`: that ban is about a *data* argument on the hot path, where a
shape-shifting `messages` complicates every call and the implementation. `optimize`
is a **construction-time switch**, where the `True/False` shortcut is the win.
`Optimizer | None` was considered and rejected: it buys nothing here, forces a
non-`None` default (`= Optimizer()`) and hence a frozen-config dance to make the
shared default instance safe, and makes `None`=off clash with the `None`=default-on
of the real ports (`telemetry=`/`secrets=`).

**The Optimizer's working state is a live in-memory aggregate, not journal data.**
It feeds off the **live event stream** — every `Telemetry.record(call)` updates
rolling per-(llm, operation) stats in memory (the Optimizer interposes at the
`record()` seam, e.g. as a `Telemetry` decorator, so this works with *any* backend
including `log()`/`none()`). The append-only journal (`Call` rows) stays the
durable source of truth; the Optimizer's rankings/tuning are a derived projection
of it. That projection **may** be checkpointed to its **own** table for a fast warm
start — but is never written back into the append-only `llmbroker_calls` (mixing a
mutable projection into an event log is a category error). Whether to checkpoint or
simply recompute from the journal on start is a **P4 open question**, not a P1
lock. Either way, `Call` must be rich from day one: a column added later starts
with no history, and historical warm-start/backfill is exactly what a queryable
backend buys.

**What it does automatically (the point):**

- **Parameter tuning** — per-LLM cooldown/delay: escalate on repeated 429s up to a
  max, decrease on sustained success, offline an LLM that keeps failing and probe
  it for recovery. The tuning state model:

  | Current state | Event       | New state | Delay adjustment            |
  |---------------|-------------|-----------|-----------------------------|
  | Available     | Error 429   | Cooling   | `current_delay` (up to Max)  |
  | Cooling       | Success     | Available | Decrease delay              |
  | Cooling       | Fail @ Max  | Offline   | Start Offline Sleep / Alarm  |
  | Offline       | Sleep End   | Probing   | Send test request           |
  | Probing       | Success     | Available | Reset to Initial Delay      |
  | Probing       | Failure     | Offline   | Restart Sleep / Alarm        |

- **Operation routing** — bias selection of each `operation` toward the LLMs that
  empirically handle it best. The policy is **tiered / lexicographic, not a
  weighted-sum scalar** (`w·quality + w·latency + w·cost` is untunable — the terms
  are not commensurable, and a latency win must never "buy back" a quality loss):
  1. **Availability gate** — candidates are LLMs not in cooldown (the FSM already
     drops Cooling/Offline); residual flakiness is a soft tiebreak.
  2. **Quality floor gate** — drop LLMs whose per-`operation` usable-rate is below a
     floor. Quality is a gate, not a tradeable term.
  3. **Objective ranking — the objective lives with the `operation`.** A background
     batch type (e.g. `receipt_classification`) ranks the gated set by quality; an
     interactive type ranks by latency. There is no single global weighting that is
     right for both.
  4. **Tokens = a budget constraint, not a quality axis.** For an identical prompt
     token counts barely differ; what matters is rate-limit budget (TPM)
     consumption → throughput headroom (a less verbose LLM yields more calls before
     a 429) and `$` when paid tiers are mixed. Tokens break ties / enforce a budget;
     they never trade against quality.

  Estimates are **confidence-aware** (bandit-style): a minimum sample count before
  an LLM's stats override round-robin, an exploration reserve so deprioritized LLMs
  keep being sampled (else their stats go stale and recovery/decay is invisible),
  and a Bayesian usable-rate for the **sparse** quality signal. The broker exposes
  a pluggable **selection policy**; the default is round-robin, and the Optimizer
  swaps in the per-`operation` ranking it maintains from telemetry. Concrete
  thresholds and the bandit flavor are a P4 open question; the tiering and the
  per-`operation`-objective principle are the decided shape.
- **Pool hygiene** — automatically deprioritize/retire consistently-useless LLMs.
  Nothing for a human to read.

**What it may use an LLM for** (optional, sampled, never on the hot path):

- **Quality judging** — **off unless the host sets `Optimizer(judge_fraction>0)`** — sample
  that fraction of outputs per (llm, operation) and score them with an LLM-as-judge,
  closing the quality loop *without* the host having to call `record_quality()`. The
  judge call goes through the broker itself (dogfooding) under a low-priority
  `operation` and **degrades gracefully** if no LLM is free — it is optional
  intelligence, never required for the broker to function, and never on by default.
- **Ambiguous tuning/routing judgement** when threshold rules are inconclusive.

**The only thing surfaced to a human** is what a human alone can fix:
`await llms.alerts()` (the broker re-exposes the Optimizer's signal; empty when
`optimize=False`) returns the rare actionable items — *the whole pool is
under-provisioned for your request rate*, *this API key looks dead* — not a feed
of trivia about individual free LLMs.

**Telemetry backend and what still works.** Two layers act independently:

- **Broker core (always on, no history):** the reactive 429/503 cooldown —
  Available↔Cooling, live `call_later` re-enqueue — runs regardless of telemetry
  backend. It reacts to live responses, not to stored history.
- **Optimizer (learned):** delay tuning, the Offline→Probing→Active recovery, and
  per-`operation` routing. It learns from the **live event stream** (in-memory
  rolling aggregates), so it is **not** gated on a queryable backend — with the
  default `Telemetry()` (log) / `NoTelemetry()` it simply boots **cold** and learns
  from live traffic.
  A **queryable** backend (`sqlite`/`jsonl`/`postgres`) is an accelerator, not a
  gate: it warm-starts those aggregates after a restart and enables ad-hoc
  analysis. This is why `operation` (and tokens/quality) are captured from P1 —
  you cannot warm-start or back-fill data you never recorded.

---

## Shipped batteries

Zero-dependency batteries live at the top level / on the port type (no import
beyond `llmbroker`). A backend that carries an external dependency is a
**submodule** you import explicitly — that import *is* the dependency.

| Port (interface) | Top-level zero-dep classes | Dependency submodules | Phase |
|---|---|---|---|
| `RegistryProtocol` | `llmbroker.Registry(path)` (file: `.toml`/`.json`) | `llmbroker.sqlite.Registry`, `llmbroker.postgres.Registry`, `llmbroker.mongodb.Registry` | registry/sqlite: P1 · pg/mongo: P3 |
| `SecretsProtocol` / `MutableSecretsProtocol` | `llmbroker.Secrets()` (env, default, read-only), `llmbroker.DictSecrets()`, callable adapter | `llmbroker.sqlite.Secrets` (mutable), `llmbroker.aws.Secrets` (mutable), `llmbroker.vault.Secrets` (mutable) | secrets/dictsecrets/sqlite.Secrets: P1 · aws/vault: P3 |
| `SharedStateProtocol` | — (default = absent, internal in-memory) | `llmbroker.redis.SharedState`, `llmbroker.postgres.SharedState`, `llmbroker.mongodb.SharedState` | seam: P1 · backends: P3 |
| `TelemetryProtocol` | `llmbroker.Telemetry()` (log, default), `llmbroker.NoTelemetry()`, `llmbroker.JsonlTelemetry(path)` | `llmbroker.sqlite.Telemetry`, `llmbroker.postgres.Telemetry`, `llmbroker.mongodb.Telemetry` | log/none/jsonl/sqlite: P1 · pg/mongo: P3 |

Composition is explicit; there is **no `from_sqlite`-style fused factory** (it
would hide the storage choice, the explicit import step, and the shared-state/
telemetry wiring). The constructor + the top-level/submodule factories are the
whole API:

```python
import llmbroker
import llmbroker.sqlite
import llmbroker.redis

llmbroker.AsyncBroker(                                      # shared_state ⇒ cluster ⇒ async
    registry=llmbroker.sqlite.Registry("broker.db"),       # seed via constructor seed= param
    shared_state=llmbroker.redis.SharedState("redis://..."),  # omit for single process
    telemetry=llmbroker.sqlite.Telemetry("broker.db"),
)
```

### Backend submodules and lazy dependencies

- **The one rule: a backend is a submodule you `import` exactly when it carries an
  external dependency.** Dependency-free batteries are top-level / on the port type
  and need only `import llmbroker` — `llmbroker.Registry(path)` (file loader,
  stdlib `tomllib`/`json` by extension), `llmbroker.Secrets()`/`DictSecrets()`,
  `llmbroker.Telemetry()`/`NoTelemetry()`/`JsonlTelemetry(path)`. There is **no**
  "eager submodule" concept and no list to memorize: if it has a dependency it is a
  submodule, if it does not it is top-level.
- Dependency-carrying backends (`sqlite`/`postgres`/`redis`/`mongodb`) are
  submodules; `llmbroker/__init__.py` never imports them, so `import llmbroker`
  stays free of every optional driver. A host does `import llmbroker.sqlite`, and
  *only then* is the driver imported.
- Each backend submodule imports its driver at module top level (`import
  aiosqlite` inside `llmbroker/sqlite.py`, `import redis` inside
  `llmbroker/redis.py`, …). Python 3 absolute imports resolve these to the real
  top-level packages, not to the same-named submodule, so there is no shadowing.
- With a future `pyproject.toml`, each dependency submodule becomes an optional
  extra (`llmbroker[sqlite]`, `llmbroker[redis]`, `llmbroker[postgres]`, …) — one
  extra per submodule.

### The `sqlite` battery owns its schema

`llmbroker.sqlite` self-manages its tables via `ensure_schema(db)`:
`llmbroker.sqlite.Registry` owns the config table `llmbroker_registry`,
`llmbroker.sqlite.Telemetry` owns `llmbroker_calls`. Its primary key is the `Call.id`
uuid (so `record_quality` can `UPDATE … WHERE id=?`). The `llmbroker_calls` schema
includes nullable token/quality columns — `prompt_tokens`, `completion_tokens`,
`total_tokens`, `usage_extra` (JSON), and `quality_score` — so the Optimizer has the
**full** `Usage` and quality history from day one (see the `Call` rationale in "Ports").
The battery persists all of `Call.usage`: the scalar token counts to their columns and
`Usage.extra` to `usage_extra` as JSON; nothing about `Usage` is dropped on persist (the
Optimizer's TPM-budget reasoning needs `total_tokens`). `ensure_schema` is the **single authority** for
the package's schema: no host migration ever builds, alters, or owns these tables
(see "Coexisting with host migration tools").

**The package maintains its own schema across releases, non-destructively.**
`ensure_schema` is idempotent and **version-aware**: it creates missing tables
and, on a DB whose `llmbroker_*` tables predate the running package version,
applies the package's own **additive, data-preserving** migrations (e.g.
`ALTER TABLE … ADD COLUMN`) — never a drop, never data loss. The schema version is
tracked in an `llmbroker_`-prefixed marker the package owns
(`llmbroker_schema_version` row / `PRAGMA user_version`), so a future release can
evolve the shape on its own cadence without touching the host's migrations. P1
ships only the initial `CREATE` plus that version marker; the upgrade path is the
seam later releases hang ALTERs off of.

dinary is the **one exception**, and only because of its pre-extraction history.
Its `llmbroker_*` tables were built by yoyo migrations `0004`/`0005` in an older
shape (a `llmbroker_providers` config table carrying the legacy
`rate_limited_until` / `execution_fail_count` columns). dinary is the package's
single local instance and that table data is disposable, so dinary's Phase 1
migration simply **drops** those tables and hands ownership to the package, which
rebuilds the current shape via `ensure_schema` on the next start (see "dinary
wiring"). This DROP is a one-off dinary cleanup of its own pre-extraction tables —
**not** the package's general upgrade story, which is the non-destructive path
above. The new `llmbroker_registry` config schema defines `name`/`base_url`/
`model`/`api_key_ref` and no `rate_limited_until`/`execution_fail_count` (live
state is in-memory now).

---

## Coexisting with host migration tools

`llmbroker` owns its tables — `llmbroker.sqlite` creates and **non-destructively
evolves** them via `ensure_schema` (see "The sqlite battery owns its schema"). The
host application almost always runs its **own** migration tool over the **same**
database. Two failure modes follow, and the package must prevent both:

1. **Name collision** — an `llmbroker` object clashing with a host object or a
   migration tool's bookkeeping table.
2. **Ownership fight** — a host autogenerate/diff tool seeing the `llmbroker`
   tables as "unknown" and emitting a `DROP` (or demanding they be modeled in the
   host's schema).

### Rule 1 — every DB object carries the `llmbroker_` prefix

Tables (`llmbroker_registry`, `llmbroker_calls`), the schema-version marker, **and
every index, unique-constraint, and trigger** the battery creates are named
`llmbroker_*`. This makes the package's whole footprint filterable by a single
prefix and collision-safe:

- Django table names are `<app>_<model>` (`auth_user`); `llmbroker_` will not collide.
- It is clear of every tool's bookkeeping table — Alembic `alembic_version`,
  yoyo `_yoyo_*`, Flyway `flyway_schema_history`, Liquibase `databasechangelog`,
  Django `django_migrations`, Aerich `aerich`.

The prefix is a public contract: host operators filter on it, and the Alembic
hook below keys off it.

### Rule 2 — tell the host's tool to leave `llmbroker_*` alone

How depends on the tool's category:

| Host tool | Category | What the host does |
|---|---|---|
| **yoyo, Flyway, Liquibase, Dbmate** | forward-only SQL runners | Nothing to fight — they only run hand-written migrations and never autogenerate. The host simply never writes a migration touching `llmbroker_*`. (dinary's one-time P1 drop migration is the deliberate exception — see "dinary wiring".) |
| **Alembic, Flask-Migrate** | autogenerate (drift) | Pass the shipped `llmbroker.alembic.include_object` hook to `context.configure` so autogenerate skips `llmbroker_*` (Flask-Migrate *is* Alembic). |
| **Aerich** | autogenerate (Tortoise) | Tortoise only manages declared models, so it emits no drop for unmodeled tables; just never model the `llmbroker_*` tables. The prefix keeps Aerich's own `aerich` table clear. |
| **Migra** | schema-diff | `migra` emits diff SQL; exclude `llmbroker_*` statements from the generated script (or diff against a baseline that already contains them). |
| **Prisma Client, Django** | ORM-managed | Each manages only its own models; an unmodeled table is left untouched. Do **not** introspect the `llmbroker_*` tables into the ORM (`inspectdb` / `prisma db pull`); if introspected, mark them unmanaged (`managed = False`) / `@@ignore`. |

### The Alembic hook (shipped, P1)

`llmbroker.alembic` is a backend-style integration submodule (analogous to
`llmbroker.sqlite`): one submodule per external tool. It ships a tiny predicate
that returns `False` for any object whose name begins with `llmbroker_`. Hosts wire
it into their `alembic/env.py`:

```python
import llmbroker.alembic

context.configure(
    connection=connection,
    target_metadata=target_metadata,
    include_object=llmbroker.alembic.include_object,   # autogenerate ignores every llmbroker_* object
)
```

If the host already passes its own `include_object`, the two compose (logical
AND — skip when either says skip). The hook imports nothing from Alembic — it only
inspects the object name — so `import llmbroker.alembic` never pulls in a migration
framework. The README documents this snippet and the per-tool table above as the
"running llmbroker alongside your migrations" section.

---

## Implementation phases

### Phase 1 — extraction + core architecture (do now)

Create `src/llmbroker/` with the async broker core `AsyncBroker` (incl. its lazy-start /
`aclose()` / `async with` lifecycle — see "Lifecycle") **and the synchronous `Broker`
wrapper** (a first-class shipped facade over `AsyncBroker` on a background event-loop
thread — see "Sync wrapper"), the ports, and the file (`.toml`/
`.json`) + `sqlite` `Registry` + `Secrets`/`DictSecrets`/`sqlite.Secrets` + internal
in-memory live state + `Telemetry`/`NoTelemetry`/`JsonlTelemetry`/`sqlite.Telemetry` batteries —
enough to serve Rung 0/1 and carry dinary with unchanged request-path behavior. The
`SharedStateProtocol` port (the cluster seam) is defined in P1; its backends land in
P3. Also capture the Optimizer's future inputs on every call — `operation`
(`ask`/`chat`), full token `usage` (from the response), and a `quality_score` written
back onto the call row by `record_quality` (matched by `Call.id`) — so the data exists
before the `Optimizer` control loop, which itself lands in Phase 4. P1 also ships the
host-coexistence surface: every DB object is `llmbroker_`-prefixed, `ensure_schema`
is version-aware (initial create now; additive data-preserving ALTERs hang off the
version marker in later releases), and `llmbroker.alembic.include_object` is
exported (see "Coexisting with host migration tools"). Because the DB schema is
**private**, P1 also ships the **broker front door** that replaces raw SQL — config
CRUD (`llms.add`/`remove` + constructor `seed=`, built on the `MutableRegistryProtocol`
backend contract), live state + usage (`await llms.snapshot()`), and call-log
read/retention (`await llms.calls()`/`purge_calls()`, built on the
`QueryableTelemetryProtocol` backend contract) — and reworks dinary's admin to consume it
(no host code calls a port directly). dinary's side gets the one-off drop migration that
hands schema ownership to the package.

```
src/llmbroker/
  __init__.py            # top-level surface — ONLY what an app uses:
                         #             AsyncBroker, Broker (sync wrapper), AsyncLLM, LLM, AsyncResult, Result,
                         #             LifecyclePhase, Optimizer, run_tool_loop, arun_tool_loop,
                         #             Registry/Secrets/DictSecrets/Telemetry/NoTelemetry/JsonlTelemetry,
                         #             LLMRequestError/NoLLMAvailable/AllLLMsFailed.
                         #             Protocols (RegistryProtocol/MutableRegistryProtocol/SecretsProtocol/SharedStateProtocol/
                         #             TelemetryProtocol/QueryableTelemetryProtocol/AsyncResourceProtocol) and DTOs (LLMConfig/
                         #             LLMState/LLMSnapshot/Usage/Call/CallStatus/LLMMetrics/Alert/SeedPolicy) are NOT exported here —
                         #             backend/admin authors import them from their defining modules (registry.py/secrets.py/
                         #             shared_state.py/telemetry.py/models.py).
                         #             NEVER imports a dep-carrying backend submodule (sqlite/redis/postgres/mongodb).
  chat.py                # from adapters/llm_chat.py — LLMConfig moves to models.py; receives the resolved
                         #             key from the broker (not off a public field); parses response usage → Usage for Call; else verbatim
                         #             also defines the tool-loop helpers `run_tool_loop(llms, messages, *, tools,
                         #             dispatch, **chat_kwargs)` and its async twin `arun_tool_loop(...)` (ported from
                         #             complete_with_tools/run_tool_step/_run_tool_loop) — host-agnostic helpers that
                         #             repeatedly call `llms.chat(messages, tools=tools, **chat_kwargs)`, execute each
                         #             `result.tool_calls` entry via the host-supplied `dispatch` mapping, append the
                         #             tool results to `messages`, and loop until a tool-call-free reply. BOTH ship from
                         #             P1: the engine is async, so `arun_tool_loop` is the real implementation and
                         #             `run_tool_loop` its sync wrapper (sync-first is about which surface a user reaches
                         #             for, never an async feature gap). Exposed at the package root as
                         #             `llmbroker.run_tool_loop` / `llmbroker.arun_tool_loop` (via __init__), so a host
                         #             never imports `llmbroker.chat` and the helper name never collides with `.chat()`.
  broker.py              # from adapters/llmbroker.py — AsyncBroker(Mapping[str, AsyncLLM]), the AsyncLLM handle
                         #             (sync .config + async .state()/.metrics()), the single front door:
                         #             ask()/chat() + `wait` capacity bound; add/remove; ensure_pool()
                         #             (seed + populate pool); snapshot(); calls/purge_calls;
                         #             alerts(); cheap __init__ + lazy start + aclose()/async with;
                         #             private _resolved_keys (name→secret) + internal LLMState + demand-driven
                         #             shared-state sync (lazy read at selection, write-through on change);
                         #             tokens/quality_score into Call; LLMRequestError/NoLLMAvailable/
                         #             AllLLMsFailed exception hierarchy; Optimizer (P1: shape only —
                         #             `Optimizer(judge_fraction=0.0)`, no control loop)
  sync.py                # Broker / LLM / Result — synchronous wrappers over Async* on a dedicated background
                         #             event-loop thread; blocking proxies (no `await`), close()/with teardown
  models.py              # LLMConfig (config: name/base_url/model/api_key_ref — no secret),
                         #             LifecyclePhase (enum), LLMState (live state + SharedState wire DTO),
                         #             LLMSnapshot (frozen config+state+metrics), SeedPolicy (Enum: MIRROR/ADD/IF_EMPTY),
                         #             Usage (provider token report), Call (llm_name/usage/…), CallStatus,
                         #             LLMMetrics (call_count/last_status/last_at),
                         #             Alert (P1 placeholder for the Optimizer's human-only signals;
                         #             alerts() always returns [] until P4 — see "Open design questions"),
                         #             AsyncResourceProtocol (shared port-lifecycle capability; aclose())
  state.py               # private in-memory per-LLM live state (always-on; not a public port) → LLMState
  schema.py              # ensure_schema for the sqlite battery: version-aware (creates + applies additive,
                         #             data-preserving ALTERs against an llmbroker_-prefixed version marker);
                         #             llmbroker_registry + llmbroker_calls, all objects llmbroker_-prefixed
  registry.py            # RegistryProtocol + MutableRegistryProtocol (admin layer) Protocols
                         #             + llmbroker.Registry file class (.toml/.json by extension; returns
                         #             pure LLMConfig — broker resolves api_key_ref)  [core, zero-dep: tomllib/json]
  secrets.py             # SecretsProtocol + MutableSecretsProtocol (admin layer) Protocols,
                         #             llmbroker.Secrets() (env, default, read-only), DictSecrets(), callable adapter,
                         #             SecretsReadOnlyError (raised by .set() on the read-only batteries)  [core]
  shared_state.py        # SharedStateProtocol Protocol (cluster seam; backends in postgres/redis/mongodb submodules)  [core]
  telemetry.py           # TelemetryProtocol + QueryableTelemetryProtocol (read layer) Protocols,
                         #             llmbroker.Telemetry() (log, default), NoTelemetry(), JsonlTelemetry(path)  [core]
  sqlite.py              # llmbroker.sqlite.Registry (config; MutableRegistryProtocol CRUD — get/add/update/remove —
                         #             used by the broker's seed application and add/remove)
                         #             + llmbroker.sqlite.Telemetry (llmbroker_calls; record + queryable read surface)
                         #             + llmbroker.sqlite.Secrets (MutableSecretsProtocol; llmbroker_secrets table)  [aiosqlite]
  alembic.py             # llmbroker.alembic.include_object — host migration-tool coexistence (dependency-free)
  cli.py                 # P1: python -m llmbroker env <config> | sync <config> --into ... --policy ... — both
                         #             offline, operate on any local TOML path (incl. presets/*.toml below).
                         #             `preset <name>` (fetch the named list from the repo's presets/ on the
                         #             default branch — see "Curated pools") ships in Phase 2.
#  (repo root, NOT under src/ — deliberately not packaged into the wheel, so a list
#   update is a plain commit independent of the package version; the Phase 2 `preset`
#   command fetches the default-branch copy)
# presets/
#   freetier.toml
#   smart-freetier.toml
```

```
tests/llmbroker/         # must NOT import dinary.*
  test_chat.py
  test_broker.py
  test_broker_sync.py
  test_registry_toml.py
  test_registry_sqlite.py
  test_secrets.py
  test_state.py
  test_telemetry.py
  test_cli_env_template.py
  test_alembic.py
```

Facts that make P1 low-risk:

- `src/` is already importable (editable install); `import llmbroker` needs **no
  pyproject/build change**. The editable install is a plain `.pth` that puts
  `src/` on `sys.path`, so a new top-level `src/llmbroker/` is importable with no
  reinstall, and deploy runs from source via `uv run` (not a built wheel). Caveat
  for later: `pyproject.toml` has no explicit `[tool.hatch.build.targets.wheel]`
  package list, so if a distributable wheel is ever built, `llmbroker` must be
  added there — not a concern for the source-based deploy now, but note it before
  any packaging work.
- `llmbroker.py` / `llm_chat.py` have **no `dinary.db` imports** today.
- `llm_storage.py`'s tables have **no FK into dinary's schema** — migration `0005`
  replaced the integer `provider_id` FK with a plain `provider_label` TEXT;
  `execution_id` is a bare TEXT correlation id. The only real coupling is
  `SqliteLLMBrokerStorage` reading `dinary.db.storage.DB_PATH` as a global instead
  of a `db_path` argument.

`src/dinary/adapters/llm_storage.py`, `llm_chat.py`, `llmbroker.py` are
**deleted**. The old SQLite/TOML storage split maps onto the new batteries:
SQLite → `llmbroker.sqlite.Registry` + `llmbroker.sqlite.Telemetry`, **no
`shared_state=`** (live state stays in the broker's internal memory); TOML →
`llmbroker.Registry` + `llmbroker.Telemetry()` (log), no shared state. Per-LLM cooldown/fail
counts are **no longer persisted** (internal in-memory now); the old JSON-sidecar
fail counter is dropped. The config record loses `rate_limited_until` (now an
`LLMState` field); the per-row identifier is `name` (dinary's old `provider_label`
maps onto it). The `api_key` columns/fields become `api_key_ref`, resolved by the
broker via `Secrets` into its private `_resolved_keys` map.

### Phase 1 — completed (v0.0.3 → v0.0.5)

Extraction done. Fixes and additions applied during stabilisation:

1. Removed dead `tried_error` flag (was always `False` at the check point).
2. A missing `api_key_ref` resolution now raises `AllLLMsFailedError` immediately
   with a message quoting the unresolved ref, instead of silently recycling the slot.
3. `_cool_down` increments `fail_count` on every 429/503.
4. `JsonlTelemetry.record()` / `record_quality()` use `asyncio.to_thread` for the
   file write so the event loop is never blocked.

`DictSecrets` added as a zero-dep test double (pre-resolved key map, no env vars).

Pool-init refactor and constructor seeding (v0.0.5):

5. Renamed `_started` → `_pool_initialized`, `_start_lock` → `_pool_lock`.
6. Replaced `ensure_started` / `_reconcile_pool` with `_populate_pool` (lock-free
   body) + `ensure_pool` (double-checked locking + seeding); removed `sync_configs`.
7. `SeedPolicy` enum (`MIRROR` / `ADD` / `IF_EMPTY`) replaces the `SyncPolicy`
   `Literal`; bare policy strings no longer used.
8. Constructor parameters `seed: RegistryProtocol | None` and
   `seed_policy: SeedPolicy = SeedPolicy.IF_EMPTY` replace the post-construction
   `sync_configs` call. Seeding happens inside `ensure_pool` on first init only,
   before the registry is loaded into the pool — secrets are therefore populated
   before resolution, eliminating duplicate "api_key_ref could not be resolved"
   warnings on restart.

### Phase 2 — example variants + catalog refresh

Add the `preset <name>` subcommand to `cli.py`. The command fetches from the
repository's `presets/` directory on the default branch:

```
https://raw.githubusercontent.com/andgineer/llmbroker/main/presets/<name>.toml
```

`presets/` lives at the repository root (not in `src/`, not bundled in the wheel)
so a list update is a plain commit independent of any package version. Add more
curated lists beyond `freetier`/`smart-freetier`. Optional: a maintainer command
that regenerates `presets/` from a documented source with latency/limits/quality
notes, committed like any other source change.

### Phase 3 — cluster + DB batteries

`llmbroker.redis`/`postgres`/`mongodb` `.shared_state`; `llmbroker.postgres`/
`mongodb` `.registry` (with the optional admin CRUD); `llmbroker.postgres`/
`mongodb` `.telemetry`; `llmbroker.aws`/`vault` `.secrets` (`MutableSecretsProtocol`
backed by AWS Secrets Manager / HashiCorp Vault KV). Each behind an optional
dependency extra.
Demand-driven sync as specified (lazy read at selection + write-through on change, no
poll); pub/sub push propagation left as a documented optimization.

### Phase 4 — the `Optimizer` (autonomous control loop)

The core value, built once telemetry capture (P1) exists. The Optimizer learns
from the **live event stream** (in-memory rolling aggregates at the
`Telemetry.record()` seam), so it runs on any backend; the **queryable read
surface** (`metrics`/`calls` — already shipped in P1 for the admin UI, on
`llmbroker.sqlite`/`jsonl` and `postgres` from P3) is for **warm-start after a
restart and ad-hoc analysis**, not a precondition. The Optimizer reuses that same
read surface rather than introducing its own, deciding warm-start vs cold-boot with
`isinstance(telemetry, QueryableTelemetryProtocol)` (the `@runtime_checkable` layer) —
not `hasattr`. Add a pluggable **selection policy**
seam to the broker (default round-robin). Build the background `Optimizer` that:
computes per-(llm, operation) stats; auto-tunes cooldowns/delays and runs the
offline→probe→active recovery (the state model in "Autonomous optimization");
maintains a per-`operation` routing ranking the broker selection consults; and
exposes `alerts()` for the human-only items (under-provisioned, dead key).
Selection strategy: first 0-wait LLM, else minimal remaining wait — biased by the
routing ranking. Default-on (`optimize=True` ≡ `Optimizer(judge_fraction=0.0)`); with the
default `Telemetry()` (log) / `NoTelemetry()` it boots cold (no warm-start) and the
broker keeps its reactive round-robin cooldown until the Optimizer has learned from
live traffic. The LLM-as-judge is enabled only by `optimize=Optimizer(judge_fraction>0)`.

### Phase 5 — LLM-in-the-loop deepening (future, not scheduled here)

The Optimizer's *optional* use of an LLM: LLM-as-judge quality scoring on sampled
outputs per (llm, operation) to close the quality loop without host `score()`, and
LLM judgement for ambiguous tuning/routing. Always sampled, off the hot path,
dogfooded through the broker under a low-priority `operation`, and gracefully
skipped when no LLM is free. Plus richer fail statistics (API-key-expiration
diagnostics) and per-LLM Initial/Min/Max delay tuning.

---

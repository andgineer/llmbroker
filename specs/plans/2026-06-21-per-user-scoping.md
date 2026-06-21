# Plan: per-user scoping via a single `user_id` on the broker

Date: 2026-06-21

> Depends on `specs/plans/2026-06-21-broker-list-interface.md` being applied
> first (the `get`/`update`/`count`/`snapshot` accessor surface is assumed
> here). This is **forward-looking** work — implement when multi-user demand is
> real, not speculatively.

## Problem

A multi-user application wants each end user to have their own LLM API keys
(and optionally their own set of LLMs), backed by one shared infrastructure
(one DB, one Redis, one Vault). Today every battery is single-tenant: the
registry, secrets, telemetry and cooldown state are global by LLM name.

The hard constraint is correctness of **cooldown state**: a cooldown is the
health of an *API key*, and keys are per-user. If state is global by name, user
A's 429 cools the LLM for user B. So key-scope and state-scope must move
together.

## Decisions (from brainstorm)

- **Single knob.** `user_id` is supplied in exactly one place —
  `Broker.__init__(user_id=...)` — and the broker threads it into every port
  call. There is no per-port `user_id` wiring to keep in sync.
- **`user_id` is request-scoped, not infrastructure.** Ports (registry,
  secrets, telemetry, state_store) are constructed once as app-lifetime
  infra. In a stateless server the broker is cheap and constructed *per
  request* with that request's `user_id`; ports are shared references.
- **A broker instance is one tenant's view.** The broker never multiplexes
  users internally (`_resolved_keys` / `_queue` stay single-user — they must,
  since resolved keys differ per user). `user_id=None` (default) = today's
  single-tenant behavior, untouched.
- **`None` is a legitimate value**, meaning "unscoped / single-tenant". The
  broker passes whatever it has — `None` or an id — and batteries decide what
  to do with it. No NULL-bucket safety is claimed (see the guard below).
- **Uniform scoping across all batteries.** `registry`, `secrets`,
  `state_store`, and `telemetry` all scope records exactly to the `user_id`
  passed. `load(uid)` returns only rows for that exact uid; `None` returns
  only unscoped rows. There is no "shared ∪ user's own" merging — mixing
  NULL and named tenants in a single query produces subtle seeding bugs that
  cannot be made correct.
- **Optional paranoia guard on `Secrets`.** `Secrets(require_user_id=True)`
  (default `False`). When set, `resolve`/`set` raise `UserScopeError` if the
  broker calls them with `user_id is None` (e.g. auth broke and silently
  yielded no user). Normal users leave it off and rely on always passing a
  real id. The same flag may be added to other sensitive batteries with the
  same name; it is never required.

## Design principles to preserve

- `user_id` flows from one field → key-scope and state-scope cannot drift.
- Per-user is a **parameter**, not a subclass. No `PerUserSqliteRegistry`:
  the same class gains a nullable `user_id` column and a `user_id=None` arg.
  The battery matrix does not grow a "× tenancy" axis.
- Pure batteries with no notion of users (`registry.Registry` file backend,
  `secrets.Secrets` env, `DictSecrets`, `NoTelemetry`, `JsonlTelemetry`)
  accept `user_id` and ignore it.

## Port protocol changes (`user_id: int | str | None = None`, default None)

### `src/llmbroker/registry.py`
- `RegistryProtocol.load(self, user_id=None) -> list[LLMConfig]`.
- `MutableRegistryProtocol`: add `user_id=None` to `get`, `add`, `update`,
  `remove`.
- File `Registry.load` accepts and ignores `user_id`.
- SQLite `Registry.load`: `WHERE user_id IS ?` — exact match, no fallback to
  NULL rows.

### `src/llmbroker/secrets.py`
- `SecretsProtocol.resolve(self, ref, user_id=None) -> str`.
- `MutableSecretsProtocol.set(self, ref, value, user_id=None) -> None`.
- New `class UserScopeError(Exception)`.
- `Secrets.__init__(self, *, require_user_id: bool = False)`. `resolve` raises
  `UserScopeError` when `require_user_id and user_id is None`; otherwise the
  env battery ignores `user_id`.
- `DictSecrets`, `_CallableSecrets`: accept and ignore `user_id`.

### `src/llmbroker/state_store.py` (renamed from `shared_state.py`)
**Rename.** Now that this port is recommended for *any* server use (state must
survive between requests, not just between cluster nodes), `shared_state` reads
as cluster-only. Rename to `state_store` while the port is protocol-only (no
backends yet) so the cost is zero. Touch points:
- file `shared_state.py` → `state_store.py`;
- `SharedStateProtocol` → `StateStoreProtocol`;
- battery classes `llmbroker.<backend>.SharedState` → `.StateStore`;
- broker port param/attr `shared_state=` / `self._shared_state` → `state_store`
  / `self._state_store` (`broker.py`, `sync.py`);
- export in `__init__.py`; mentions in `README.md`, `specs/reference/architecture.md`.

Protocol surface (with `user_id`):
- `StateStoreProtocol.read(self, user_id=None) -> dict[str, LLMState]`.
- `StateStoreProtocol.write(self, name, state, user_id=None) -> None`.
- Fix the misleading docstring: state_store preserves state **between
  requests**, not only between cluster nodes. Any stateless server (multiple
  workers, restarts, a load balancer) needs it, even on one machine. Drop the
  "cluster-only" wording.

### `src/llmbroker/telemetry.py`
- `Call` gains `user_id` (see models). `record(self, call)` already carries it
  via the dataclass.
- `QueryableTelemetryProtocol`: add `user_id=None` to `metrics`, `calls`,
  `purge_calls`.
- `NoTelemetry`, `JsonlTelemetry`: accept/ignore where applicable.

### `src/llmbroker/models.py`
- `Call`: add `user_id: int | str | None = None` (last field, default None,
  keeps the frozen dataclass back-compatible).

## Broker changes

### `src/llmbroker/broker.py`
- `AsyncBroker.__init__`: add `user_id: int | str | None = None`; store
  `self._user_id`.
- Thread it through every port call:
  - `_populate_pool`: `await self._registry.load(self._user_id)`.
  - `_apply_seed`: seed catalog loads unscoped (`load(None)` — shared defaults)
    but is written into the registry under `self._user_id`.
  - `_add_to_pool`: `await self._secrets.resolve(cfg.api_key_ref, self._user_id)`.
  - `_cool_down`: `await self._state_store.write(config.name, state, self._user_id)`.
  - `snapshot` / `AsyncLLM.state`: `await self._state_store.read(self._user_id)`.
  - telemetry: build `Call(..., user_id=self._user_id)` in `chat`;
    `metrics(since=..., user_id=self._user_id)`.
  - `add`/`update`/`remove`/`get` → pass `self._user_id` to the registry.
- `AsyncLLM.__init__`: receive `user_id` and use it in `state()` / `metrics()`.

### `src/llmbroker/sync.py`
- `Broker.__init__`: mirror `user_id=...`, forward to `AsyncBroker`.

## SQLite battery + schema

### Gotcha: sole PRIMARY KEY breaks per-user
`llmbroker_registry(name TEXT PRIMARY KEY)` and
`llmbroker_secrets(ref TEXT PRIMARY KEY)` cannot hold the same name/ref for two
users. Per-user needs uniqueness on `(user_id, name)` / `(user_id, ref)`.

SQLite treats NULLs as distinct in UNIQUE constraints, so a plain
`UNIQUE(name, user_id)` would let two `user_id IS NULL` rows share a name —
breaking single-tenant uniqueness. Enforce uniqueness over a normalized key,
e.g. `UNIQUE(name, COALESCE(user_id, ''))` via a generated column or an
expression index, and keep the broker's own `_configs` check as the first line.

### `src/llmbroker/schema.py`
No production installations exist, so no migration path is needed. The new
schema (with `user_id` columns and expression indexes) is the only schema.
`_SCHEMA_VERSION` stays at 1; `PRAGMA user_version` guard and the 12-step
table rebuild are not required.

### `src/llmbroker/sqlite.py`
- Every method takes `user_id=None`.
- `Registry`: `load` → `WHERE user_id IS :uid` (exact match); `get`/`add`/
  `update`/`remove` scope by `user_id` (write the user's own rows).
- `Secrets`: `resolve` → `WHERE ref = :ref AND user_id IS :uid` (exact match,
  **no** `IS NULL` fallback — a missing per-user row is `KeyError`, never the
  shared row); `set` upserts under `(ref, user_id)`. Also honor
  `require_user_id`.
- `Telemetry`: `record` writes `user_id`; `metrics`/`calls`/`purge_calls` add
  `WHERE user_id IS :uid` when a uid is given.
- `_call_from_row` / INSERT column lists: include `user_id`.

### Alembic
- `src/llmbroker/alembic.py`: matching migration for the Postgres path.

## Tests (same session as implementation)

- `tests/test_secrets.py`: `resolve`/`set` round-trip under two distinct
  `user_id`s stay isolated; `require_user_id=True` + `user_id=None` raises
  `UserScopeError`; `require_user_id=False` + `None` resolves the unscoped row.
- `tests/test_broker.py`: two brokers with different `user_id` over one shared
  set of batteries see independent keys, cooldowns and snapshots; a cooldown
  written under user A is invisible to user B; `user_id=None` reproduces
  current single-tenant behavior.
- `tests/test_registry`/sqlite: per-user rows isolated; `load(None)` returns
  only unscoped rows; same `name` allowed across users; duplicate within one
  user rejected.
- `tests/test_sync.py`: `Broker(user_id=...)` forwards correctly.

## Docs

- `docs/src/{en,ru}/`: a "Multi-user" section — ports are app-lifetime infra,
  the broker is constructed per request with `user_id`; all batteries scope
  records exactly to `user_id` (`None` = unscoped / single-tenant); the
  `require_user_id` guard as opt-in paranoia.
- Architecture note (and the corrected `state_store` docstring): stateless
  servers need `state_store` to keep cooldowns **between requests**, not only
  between cluster nodes.

## Release

- Additive, back-compatible API (new optional params, default `None`). Bump
  with `invoke ver-feature`.

## Done gate

1. `invoke pre` — clean (ruff + pyrefly).
2. `python -m pytest` — all pass (doctests included via `--doctest-modules`).

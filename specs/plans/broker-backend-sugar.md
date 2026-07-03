# `stack=` constructor sugar for `Broker`/`AsyncBroker`

## Goal

Replace this:

```python
registry   = llmbroker.sqlite.Registry("broker.db")
secrets    = llmbroker.sqlite.Secrets("broker.db")
telemetry  = llmbroker.sqlite.Telemetry("broker.db")
broker = llmbroker.Broker(registry=registry, secrets=secrets, telemetry=telemetry)
```

with:

```python
broker = llmbroker.Broker(stack=llmbroker.sqlite.Stack("broker.db"))
```

while still allowing per-port overrides (mixed backends), including disabling
a port the stack would otherwise provide (`state_store=None`).

## Design decisions (settled in discussion)

1. **Typed, not stringly-dispatched.** `stack=` takes an object, never a
   `store="sqlite"` string — keeps pyrefly strict typing and IDE completion
   intact, no internal string→class registry.
2. **`BackendStack` is a `Protocol`, not a dataclass base class.** This
   codebase already models every port (`RegistryProtocol`, `SecretsProtocol`,
   `TelemetryProtocol`, `StateStoreProtocol`) as a structural `Protocol` with
   no shared base implementation. `Stack` classes follow the same shape:
   plain independent classes with four attributes, no inheritance.
3. **Eager construction inside `Stack.__init__` is fine, not lazy.** Checked
   every backend's port constructor (`sqlite`/`postgres`/`mongodb`,
   registry/secrets/telemetry/state_store, 12 classes total): every one of
   them only stores a path string or an already-built shared connection
   object (`self._db_path = str(db_path)` / `self._pool = pool` / `self._db = db`).
   No I/O, no `ensure_schema`, no connection handshake happens in any
   `__init__` — that all happens lazily per call, inside each async method,
   exactly as it does today. So a `Stack` port that later gets overridden away
   by an explicit `Broker(..., secrets=other)` never did anything before being
   discarded. Building `functools.cached_property`-style laziness here would
   guard against a cost that provably does not exist — skipped as unwarranted
   complexity.
4. **Only `state_store` needs a sentinel default.** `registry`/`secrets`/`telemetry`
   never had a legitimate "explicitly `None`" meaning — `None` has only ever
   meant "not passed, use the zero-dependency default" — so plain `is None`
   checks are enough for those three, same as today. `state_store` is
   different: `state_store=None` already means "deliberately disabled" today.
   Once `stack=` can also supply a `state_store`, `None` must be able to mean
   both "not passed, use the stack's" and "explicitly disabled" — impossible
   to tell apart without a real sentinel default distinct from `None`.
5. **No `profile_path=`/`persist_profile=` additions to `Broker`.** Out of
   scope — adds validation/error-path surface for a case that already has a
   one-line workaround today (`registry=llmbroker.Registry(path, profile_path=...)`).
   Not part of this change.
6. **No `standalone.Stack`, no `Stack` for `redis`/`aws`/`vault`.** The bare
   TOML-path shortcut already covers the standalone family; `redis`/`aws`/`vault`
   are single-port backends and stay override-only via their existing
   constructors (`redis.StateStore.from_url(url)`, `aws.Secrets(...)`, `vault.Secrets(...)`).
7. **No DSN-string convenience for `postgres.Stack`.** `asyncpg.create_pool(dsn)`
   is async; `Broker.__init__` is sync. The caller builds the pool once,
   outside `Broker.__init__`, and hands the `Pool` object to `Stack`. This is
   a physical constraint, not a design choice. `mongodb.Stack` has no such
   constraint — `AsyncIOMotorClient(url)[db_name]` is synchronous — so a Mongo
   caller can go straight from a URL to `Stack` with no async step.

## API shape

### 1. `BackendStack` protocol

New file `src/llmbroker/protocols/backend_stack.py`:

```python
"""BackendStack contract: a bundle of registry/secrets/telemetry/state_store
built from one shared connection. Implement this shape (four attributes) to
wire your own — see llmbroker.sqlite.Stack / llmbroker.postgres.Stack /
llmbroker.mongodb.Stack for reference implementations.
"""

from typing import Final, Protocol

from llmbroker.protocols.registry import RegistryProtocol
from llmbroker.protocols.secrets import SecretsProtocol
from llmbroker.protocols.state_store import StateStoreProtocol
from llmbroker.protocols.telemetry import TelemetryProtocol


class BackendStack(Protocol):
    registry: RegistryProtocol
    secrets: SecretsProtocol
    telemetry: TelemetryProtocol
    state_store: StateStoreProtocol | None


class _UnsetType:
    """Sentinel type for `state_store`'s default — distinguishes "not passed"
    from the already-meaningful `state_store=None` ("explicitly disabled").
    Defined once here so `broker/broker.py` and `sync.py` compare against the
    exact same singleton.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "<unset>"


UNSET: Final = _UnsetType()
```

### 2. `Stack` class per multi-port backend package

Only `sqlite`, `postgres`, `mongodb` get one — the three packages that
implement all three mandatory ports (registry, secrets, telemetry).

`src/llmbroker/sqlite/__init__.py` — add:

```python
class Stack:
    """One SQLite file backing registry, secrets, telemetry, and state store."""

    def __init__(self, db_path: str | Path, *, require_user_id: bool = False) -> None:
        self.registry = Registry(db_path)
        self.secrets = Secrets(db_path, require_user_id=require_user_id)
        self.telemetry = Telemetry(db_path)
        self.state_store = StateStore(db_path)
```

(needs `from pathlib import Path` added to the module; add `"Stack"` to `__all__`)

`src/llmbroker/postgres/__init__.py` — add:

```python
class Stack:
    """One asyncpg pool backing registry, secrets, telemetry, and state store.

    Build the pool yourself first — pool creation is async, `Broker.__init__`
    is sync: ``pool = await asyncpg.create_pool(dsn)``.
    """

    def __init__(self, pool: asyncpg.Pool, *, require_user_id: bool = False) -> None:
        self.registry = Registry(pool)
        self.secrets = Secrets(pool, require_user_id=require_user_id)
        self.telemetry = Telemetry(pool)
        self.state_store = StateStore(pool)
```

(needs `import asyncpg` added; add `"Stack"` to `__all__`)

`src/llmbroker/mongodb/__init__.py` — add:

```python
class Stack:
    """One Mongo database backing registry, secrets, telemetry, and state store."""

    def __init__(self, db: AsyncIOMotorDatabase, *, require_user_id: bool = False) -> None:
        self.registry = Registry(db)
        self.secrets = Secrets(db, require_user_id=require_user_id)
        self.telemetry = Telemetry(db)
        self.state_store = StateStore(db)
```

(needs `from motor.motor_asyncio import AsyncIOMotorDatabase` added; add `"Stack"` to `__all__`)

### 3. `AsyncBroker.__init__` changes (`src/llmbroker/broker/broker.py`)

New imports: `from llmbroker.protocols.backend_stack import UNSET, BackendStack, _UnsetType`.

Signature changes:

```python
def __init__(  # noqa: PLR0913
    self,
    registry: RegistryProtocol | str | Path | None = None,
    *,
    stack: BackendStack | None = None,
    secrets: SecretsProtocol | None = None,
    state_store: StateStoreProtocol | None | _UnsetType = UNSET,
    telemetry: TelemetryProtocol | None = None,
    optimize: bool | Optimizer = True,
    seed: RegistryProtocol | str | Path | None = None,
    seed_policy: SeedPolicy = SeedPolicy.SYNC,
    user_id: int | str | None = None,
) -> None:
```

Resolution, replacing the current unconditional
`registry = Registry(registry) if isinstance(registry, (str, Path)) else registry`
and the `secrets`/`telemetry` defaulting lines:

```python
if registry is None and stack is None:
    raise ValueError("AsyncBroker requires either `registry` or `stack`")

if registry is None:
    assert stack is not None
    registry = stack.registry
elif isinstance(registry, (str, Path)):
    registry = Registry(registry)

secrets = (
    as_secrets(secrets)
    if secrets is not None
    else (stack.secrets if stack is not None else Secrets())
)
telemetry = telemetry if telemetry is not None else (
    stack.telemetry if stack is not None else Telemetry()
)

resolved_state_store: StateStoreProtocol | None
if isinstance(state_store, _UnsetType):
    resolved_state_store = stack.state_store if stack is not None else None
else:
    resolved_state_store = state_store
```

Every later use of the old `state_store` parameter in the body (currently:
`self._state_store = state_store`, the `LLMPool(state_store, ...)` call, and
the `_ProfileSync(registry, state_store, ...)` call) switches to
`resolved_state_store`.

### 4. `sync.Broker.__init__` changes (`src/llmbroker/sync.py`)

Mirrors the signature exactly (same pattern the file already follows for
every other param) and forwards straight through — `AsyncBroker` does all the
resolution, so `sync.Broker` does not need `_UnsetType` logic beyond typing
its own default the same way:

```python
def __init__(  # noqa: PLR0913
    self,
    registry: RegistryProtocol | str | Path | None = None,
    *,
    stack: BackendStack | None = None,
    secrets: SecretsProtocol | None = None,
    state_store: StateStoreProtocol | None | _UnsetType = UNSET,
    telemetry: TelemetryProtocol | None = None,
    optimize: bool | Optimizer = True,
    seed: RegistryProtocol | str | Path | None = None,
    seed_policy: SeedPolicy = SeedPolicy.SYNC,
    user_id: int | str | None = None,
) -> None:
    if isinstance(registry, (str, Path)):
        registry = Registry(registry)
    ...
    fut.set_result(
        AsyncBroker(
            registry,
            stack=stack,
            secrets=secrets,
            state_store=state_store,
            telemetry=telemetry,
            ...
        ),
    )
```

Add `from llmbroker.protocols.backend_stack import UNSET, BackendStack, _UnsetType`
to its imports.

### Usage after this change

```python
# everything sqlite, one line
Broker(stack=llmbroker.sqlite.Stack("broker.db"))

# everything mongodb, state via Redis
Broker(stack=llmbroker.mongodb.Stack(db), state_store=llmbroker.redis.StateStore.from_url(url))

# everything sqlite, secrets via Vault
Broker(stack=llmbroker.sqlite.Stack("broker.db"), secrets=llmbroker.vault.Secrets(url=url, token=token))

# everything sqlite, no state store
Broker(stack=llmbroker.sqlite.Stack("broker.db"), state_store=None)

# postgres — pool built once, outside Broker
pool = await asyncpg.create_pool(dsn)
async with llmbroker.AsyncBroker(stack=llmbroker.postgres.Stack(pool)) as llms:
    ...
```

## Files touched

- `src/llmbroker/protocols/backend_stack.py` — new: `BackendStack` protocol, `_UnsetType`/`UNSET`.
- `src/llmbroker/sqlite/__init__.py` — add `Stack`.
- `src/llmbroker/postgres/__init__.py` — add `Stack`.
- `src/llmbroker/mongodb/__init__.py` — add `Stack`.
- `src/llmbroker/broker/broker.py` — `AsyncBroker.__init__`: `stack` param,
  `registry` becomes optional, sentinel-based `state_store` resolution.
- `src/llmbroker/sync.py` — `Broker.__init__`: mirror the same new params and
  forward them.
- `docs/src/en/usage.md` / `docs/src/ru/usage.md` — update the "SQLite backend"
  and "Multi-user" examples to use `stack=`; add a short "Mixing backends"
  example (Postgres + Redis) per the mockup validated in discussion.
- No change to `src/llmbroker/__init__.py` — `Stack` stays namespaced under
  each backend package (`llmbroker.sqlite.Stack`, etc.), matching how
  `Registry`/`Secrets`/`Telemetry` are namespaced today. `BackendStack` is not
  re-exported at top level (YAGNI — add if a real need for the type hint shows up).

## Tests to add

New `tests/broker/test_stack.py` (or alongside existing broker construction
tests):

- `sqlite.Stack(path)` wires all four ports from one file — round-trip a
  config through registry, resolve a secret, record a call, read state.
- `stack=` + explicit `secrets=` override wins over the stack's secrets.
- `stack=` + explicit `state_store=None` disables it (vs. the stack's default).
- `stack=` + explicit `state_store=<other backend>` wins (mixed families, e.g.
  `mongodb.Stack` + `redis.StateStore`).
- `registry=None, stack=None` raises `ValueError`.
- bare-path shortcut (`Broker("llms.toml")`) still works unchanged.
- `postgres.Stack(pool)` / `mongodb.Stack(db)` wired correctly (docker-marked,
  reuse `pg_pool`/`mongo_db` fixtures from `tests/conftest.py`).
- Same override + disable cases mirrored for `sync.Broker` (thin forwarding
  layer — no need to duplicate every case).

## Verification

`invoke pre` and `python -m pytest` both green per repo's non-negotiable done
gate before calling this done.

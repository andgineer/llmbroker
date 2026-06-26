# Plan: Redis StateStore

**Source of truth:** https://github.com/andgineer/llmbroker/issues/1

## Goal

Add `llmbroker.redis.StateStore` implementing `StateStoreProtocol` so a cluster of workers
shares a single view of LLM cooldown state.

---

## Data model

One Redis hash per `(user_id)` scope:

- Key: `llmbroker_state:__none__` for `user_id=None`; `llmbroker_state:{user_id}` for any other value
- Fields: `{llm_name}` → JSON-encoded `{"phase": "...", "cooldown_until": "<ISO>" | null, "fail_count": N}`

`read()` → single `HGETALL`; `write()` → single `HSET`.

Phase-derivation logic is identical to `llmbroker.sqlite.StateStore`:
- `OFFLINE` / `PROBING` → trust stored phase as-is; clear expired `cooldown_until`
- `COOLING` with future `cooldown_until` → `COOLING`
- `COOLING`/`AVAILABLE` with past-or-no `cooldown_until` → `AVAILABLE`, `cooldown_until=None`
- anything else → `ValueError`

---

## Files to create / change

### `pyproject.toml`

Add optional-dependency extra (no such section exists yet):

```toml
[project.optional-dependencies]
redis = ["redis>=5.0"]
```

`redis>=5.0` ships `redis.asyncio` as a submodule — no separate `aioredis` needed.

### `src/llmbroker/redis/__init__.py`

```python
"""Redis backend — StateStore over Redis hashes.

Needs the ``redis`` driver (``llmbroker[redis]``); importing this package
declares that dependency, so a bare ``import llmbroker`` stays driver-free.
All keys are ``llmbroker_``-prefixed.
"""

from llmbroker.redis.state_store import StateStore

__all__ = ["StateStore"]
```

### `src/llmbroker/redis/state_store.py`

Top-level imports only (no local imports per project rules):

```python
import json
from datetime import UTC, datetime
from typing import Self

import redis.asyncio as aioredis

from llmbroker.models import LifecyclePhase, LLMState, check_user_id
```

Constructor accepts a pre-built async client; class method for URL construction:

```python
class StateStore:
    def __init__(self, client: aioredis.Redis) -> None:
        self._client = client

    @classmethod
    def from_url(cls, url: str, **kwargs: object) -> Self:
        return cls(aioredis.from_url(url, decode_responses=True, **kwargs))
```

`_scope_key(user_id)` → `"llmbroker_state:__none__"` when `None`, else
`f"llmbroker_state:{user_id}"`.

> **Known limitation:** the sentinel `__none__` collides with a literal `user_id="__none__"`.
> `check_user_id` rejects only empty strings, so a caller with that exact string would read/write
> the wrong scope. Document in the class docstring; acceptable for now.

`read(user_id=None)`:
1. `check_user_id(user_id)`
2. `raw = await self._client.hgetall(self._scope_key(user_id))`
3. Deserialize each field value from JSON, apply phase-derivation, build `dict[str, LLMState]`.

`write(name, state, user_id=None)`:
1. `check_user_id(user_id)` + timezone check on `cooldown_until`
2. Build JSON payload (mirror the `cooldown_iso` logic from the SQLite store: omit `cooldown_until` when phase is AVAILABLE).
3. `await self._client.hset(self._scope_key(user_id), name, json.dumps(payload))`

`aclose()`:
```python
async def aclose(self) -> None:
    await self._client.aclose()
```

> **Known limitation:** Redis keys have no TTL. COOLING entries that are never overwritten
> accumulate indefinitely. Acceptable for v1; a future iteration can set a TTL on `hset` equal to
> the maximum possible cooldown window.

---

## Tests

### `tests/test_redis_state_store.py`

Add `fakeredis>=2.26` to the `dev` dependency group in `pyproject.toml`.

```python
import asyncio
import fakeredis.aioredis

import llmbroker.redis
from llmbroker.models import LifecyclePhase, LLMState
```

Each test creates `fakeredis.aioredis.FakeRedis(decode_responses=True)` and passes it to
`StateStore(client)`. Tests are synchronous functions that call `asyncio.run()` (same pattern as
SQLite tests in `test_state.py` — no `pytest-asyncio`).

Mirror the pure-`StateStore` test cases from `tests/test_state.py` (the broker integration test
`test_cooldown_persisted_in_sqlite_survives_broker_restart` is excluded — it requires a broker
wired with a Redis store and is better covered by a future integration test suite):

| Test name | What it checks |
|---|---|
| `test_redis_state_store_read_empty` | fresh store → `{}` |
| `test_redis_state_store_write_and_read_available` | round-trip AVAILABLE |
| `test_redis_state_store_write_and_read_cooling` | round-trip COOLING with future timestamp |
| `test_redis_state_store_expired_cooling_reads_as_available` | past `cooldown_until` → AVAILABLE |
| `test_redis_state_store_overwrite` | second write on same name replaces first |
| `test_redis_state_store_per_user_isolated` | alice's entry invisible to bob and unscoped |
| `test_redis_state_store_user_id_none_unscoped` | `None` scope returns only NULL-scoped entries |
| `test_redis_state_store_offline_phase` | OFFLINE round-trip: phase trusted as-is |

---

## Verification steps

1. `invoke pre` → zero ruff / pyrefly errors
2. `python -m pytest tests/test_redis_state_store.py -v` → all green
3. `python -m pytest` → no regressions
4. Bare-import check — `redis` must not appear in `sys.modules` after `import llmbroker`:
   ```
   python -c "import llmbroker, sys; assert 'redis' not in sys.modules"
   ```

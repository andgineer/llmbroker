# Plan: Share cooldown state at LLM-selection time

Closes https://github.com/andgineer/llmbroker/issues/4

## Goal

When process A cools an LLM after a 429, process B should skip that LLM at the
next selection point instead of earning a redundant 429. The fix is a short-lived
in-process cache of shared `StateStore` data, consulted just before each LLM is
used — no background timer, no polling, zero reads when the broker is idle.

---

## Scope of changes

### `src/llmbroker/broker/pool.py`

**1. Module-level constant**

```python
_SHARED_CACHE_TTL = 2.0  # seconds
```

**2. `LLMPool.__init__` — two new fields**

```python
self._shared_cache: dict[str, LLMState] | None = None
self._shared_cache_expires: float = 0.0
```

**3. New private method `_get_shared_cache`**

```python
async def _get_shared_cache(self) -> dict[str, LLMState]:
    now = time.monotonic()
    if self._shared_cache is not None and now < self._shared_cache_expires:
        return self._shared_cache
    self._shared_cache = await self.stored_states()
    self._shared_cache_expires = now + _SHARED_CACHE_TTL
    return self._shared_cache
```

**4. New private method `_apply_shared_cooling`**

The method is private (`_` prefix) — it is an internal broker coordination detail, not
part of the public `LLMPool` API. `router.py` can still call it via `self._pool`.

```python
async def _apply_shared_cooling(self, config: LLMConfig) -> bool:
    """Return True if shared state shows this LLM cooling and the slot was deferred.

    Syncs local InMemoryState and re-schedules the slot for the correct
    re-queue time so this process doesn't re-probe until the cooldown expires.
    Returns False on any store error so the caller proceeds normally.
    """
    if self._state_store is None:
        return False
    try:
        shared = await self._get_shared_cache()
    except Exception:
        logger.warning("LLM %s: shared-state read failed; proceeding without shared cooling", config.name)
        return False
    stored = shared.get(config.name)
    if stored is None or stored.phase is not LifecyclePhase.COOLING:
        return False
    if stored.cooldown_until is None:
        return False
    now_utc = datetime.now(UTC)
    if stored.cooldown_until <= now_utc:
        return False
    local_fail_count = self._state.fail_count(config.name)
    self._state.set_cooling(config.name, stored.cooldown_until, max(local_fail_count, stored.fail_count))
    delay = (stored.cooldown_until - now_utc).total_seconds()
    asyncio.get_running_loop().call_later(delay, self._queue.put_nowait, config)
    logger.info("LLM %s: shared cooldown applied, %.0fs remaining", config.name, delay)
    return True
```

`fail_count` is set to `max(local, stored)` to preserve any quality-fail history that
the local process accumulated via `mark_quality_fail` (not written to the store).

**5. `cool_down` — invalidate cache on write**

Add one line immediately after the `await self._state_store.write(...)` call:

```python
self._shared_cache = None
```

Rationale: invalidate after the write, not before. Placing the invalidation
before the `await` leaves a window where a concurrent coroutine can refresh the
cache from the store between the `None` assignment and the write completing,
caching the pre-write (stale) state for up to `_SHARED_CACHE_TTL` seconds.

**6. Add `import time` at the top** (already has `asyncio`, `datetime`, etc.).
Also import `LifecyclePhase` from `llmbroker.models` (not yet imported in pool.py).

No new import is needed for `stored_states` — it already exists as a method on `LLMPool`.

---

### Known limitations (conscious tradeoffs)

**`_SHARED_CACHE_TTL` is not configurable.** 2 s is a single hardcoded constant.
Different store backends (Redis vs SQLite) may have very different read latencies, but
making TTL a constructor parameter is deferred. Change the constant if needed.

**Concurrent cache refresh.** Two coroutines that simultaneously see an expired cache
will both call `stored_states()`. The second write is harmless (same data, same result),
but amortisation is lost for that burst. Adding an asyncio lock is not worth the
complexity at current scale.

---

### `src/llmbroker/broker/router.py`

In `Router.chat`, after the stale-slot guard and before the key check, add:

```python
# Existing:
if config.name not in self._pool:
    continue

# NEW — skip slots another process already rate-limited
if await self._pool._apply_shared_cooling(config):
    continue

# Existing:
if not self._pool.has_key(config.name):
    ...
```

No other changes to `router.py`.

---

## Tests — `tests/test_pool.py`

Add a new test section `# --- shared-state cache ---`.

### Unit tests for `_apply_shared_cooling`

| Test | Setup | Expected |
|---|---|---|
| `test_apply_shared_cooling_no_store` | `LLMPool(state_store=None, ...)` | returns `False`, no error |
| `test_apply_shared_cooling_store_error` | store `read` raises `Exception` | returns `False`, logs warning, no propagation |
| `test_apply_shared_cooling_not_in_shared` | store returns `{}` | returns `False` |
| `test_apply_shared_cooling_available` | store returns `AVAILABLE` state | returns `False` |
| `test_apply_shared_cooling_cooling_no_until` | store returns `COOLING`, `cooldown_until=None` | returns `False` |
| `test_apply_shared_cooling_expired` | store returns `COOLING`, `cooldown_until` 5 s ago | returns `False` |
| `test_apply_shared_cooling_active` | store returns `COOLING`, `cooldown_until` 30 s ahead | returns `True`, local state updated, slot re-scheduled |
| `test_apply_shared_cooling_preserves_local_fail_count` | local `fail_count=5`, store returns `fail_count=2` | after call, `pool.state(name).fail_count == 5` |

For `test_apply_shared_cooling_active`: after `_apply_shared_cooling` returns `True`, verify
`pool.state(name).phase == LifecyclePhase.COOLING` and `pool.state(name).cooldown_until` matches.

### Cache TTL tests

| Test | Setup | Expected |
|---|---|---|
| `test_shared_cache_hit` | call `_get_shared_cache` twice within TTL | store `read` called exactly once |
| `test_shared_cache_expires` | call `_get_shared_cache`, advance `time.monotonic` past TTL, call again | store `read` called twice |
| `test_cool_down_invalidates_cache` | warm the cache, call `cool_down`, check `_shared_cache is None` | cache is None |

Use `unittest.mock.AsyncMock` for the store; patch `time.monotonic` with a
controllable float for TTL tests.

### Integration test — two pools, one store

```
test_cross_process_cooldown_shared_via_store
```

1. Create a real `sqlite.StateStore` (tmp path via `pytest`'s `tmp_path` fixture).
2. Create pool A and pool B, both backed by the same store.
3. Add LLM "x" to both pools.
4. Call `await pool_a.cool_down(cfg_x, httpx.Headers({"retry-after": "30"}))` — writes COOLING to store.
5. Dequeue the slot from pool B before testing: `cfg_dequeued = await pool_b.acquire(0)`.
   This simulates the router acquiring the slot before calling `_apply_shared_cooling`.
   Without this step, `call_later` inside `_apply_shared_cooling` would add a second slot
   to pool B's queue, creating a duplicate.
6. Call `await pool_b._apply_shared_cooling(cfg_dequeued)` — must return `True`.
7. Assert `pool_b.state("x").phase == LifecyclePhase.COOLING`.

---

## Verification checklist

- [ ] `invoke pre` — zero errors (ruff, pyrefly, pre-commit)
- [ ] `python -m pytest` — all pass, no new failures
- [ ] Doctests in modified source files are up to date

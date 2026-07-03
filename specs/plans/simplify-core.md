# Simplify the core substrate (Plan 1 of 3)

Replaces the `asyncio.Queue` routing substrate with a slot table, makes per-LLM
concurrency configurable (the one deliberate behavior change), and removes the
sync construction dance. Companion plans, in execution order after this one:
`specs/plans/simplify-learning.md` (Plan 2), `specs/plans/simplify-storage.md`
(Plan 3).

Except step 1.2 (explicitly a behavior change), everything preserves behavior
bit-for-bit as specified in `specs/reference/architecture.md` and
`specs/reference/optimizer.md`. Public API stays intact.

## Rules for the implementer (read first)

- Lint/format/type-check only via `invoke pre` (never call ruff directly). Tests via
  `python -m pytest`. Both must be green after **every numbered step**.
- `pytest.ini` runs `--doctest-modules`: doctests in `src/` execute as tests. If you
  touch code near a doctest, keep it correct.
- No in-function imports; no `from __future__ import annotations`; Python 3.11+.
- Never edit `src/llmbroker/__about__.py`; never bump the version.
- Never use `pytest.skip`/`importorskip`/`skipIf`. Testcontainers run locally.
- Never delete a test that reproduces a confirmed bug; port it to the new surface.
- Do not change optimizer math or policy in this plan (Plan 2 owns that).
- If tests fail in an area a step should not affect: stop and reassess; do not
  paper over with assertion changes.
- Comments: 1–2 lines, non-obvious WHY only. No refactor narration.

---

## 1.1 Replace the `asyncio.Queue` routing substrate with a slot table

**Problem.** `LLMPool` (`src/llmbroker/broker/pool.py`) models "at most one in-flight
request per LLM" as an `asyncio.Queue`. Since `SelectionPolicy` arrived, `acquire()`
drains the whole queue, ranks, picks one, re-enqueues the rest (`pool.py:234-265`).
The queue drags along `loop.call_later` re-enqueue timers (`pool.py:299,356`) —
loop-bound state that forces `sync.py`'s cross-thread construction dance — plus the
drain in `set_benched` (`pool.py:127-144`), `_reenqueue_config` guards, and the
stale-slot / defensive-keyless paths in `Router.chat` (`router.py:86-98`).

This step is behavior-preserving: `in_flight` is a bool here; step 1.2 turns it
into a counter.

**New data model** (replaces the queue, `InMemoryState`, and the marker structures
`_benched`, `_deprecated`, `_resolved_keys`; `_demoted_operations` /
`_globally_demoted` may stay as dicts or move onto the slot — single source only):

```python
@dataclass
class _Slot:
    config: LLMConfig
    key: str | None = None
    in_flight: bool = False
    cooldown_until: datetime | None = None   # aware UTC
    fail_count: int = 0
    benched: bool = False
    deprecated: bool = False
    pick_seq: int = 0                        # monotone counter for round-robin
```

`LLMPool.__init__` gains `self._slots: dict[str, _Slot]`, `self._cond =
asyncio.Condition()` (binds to the loop lazily on first await — safe to create off
loop in 3.11), `self._pick_counter = 0`. Delete `broker/state.py` (`InMemoryState`);
`pool.state(name)` derives `LLMState` from the slot exactly like `models.reconcile`:
COOLING iff `cooldown_until is not None and cooldown_until > now`, else AVAILABLE
with `cooldown_until=None`; `fail_count` passed through.

**Availability predicate** (private helper, used by `acquire` and tests):

```python
def _available(self, slot: _Slot, now: datetime) -> bool:
    return (
        slot.key is not None
        and not slot.benched
        and not slot.in_flight
        and (slot.cooldown_until is None or slot.cooldown_until <= now)
    )
```

**`acquire(wait, *, policy=None, operation=None)`** — full logic:

```python
deadline = None if wait is None else time.monotonic() + wait
async with self._cond:
    while True:
        now = datetime.now(UTC)
        avail = [s for s in self._slots.values() if self._available(s, now)]
        if avail:
            best_tier, candidates = self._partition_by_tier(avail, operation)
            picked_cfg = policy.select([s.config for s in candidates], operation=operation) if policy else None
            slot = self._slots[picked_cfg.name] if picked_cfg else min(candidates, key=lambda s: s.pick_seq)
            slot.in_flight = True
            self._pick_counter += 1
            slot.pick_seq = self._pick_counter
            self._maybe_alert_degraded_tier(slot.config.name, operation, best_tier)
            return slot.config
        if wait == 0:
            raise TimeoutError("no LLM slot available and wait=0")
        timeout = self._wake_timeout(now, deadline)   # see below
        if deadline is not None and timeout is not None and timeout <= 0:
            raise TimeoutError("no LLM slot came free within wait")
        try:
            await asyncio.wait_for(self._cond.wait(), timeout)
        except TimeoutError:
            continue   # re-check: a cooldown may have expired, or deadline hit (next loop raises)
```

`_wake_timeout(now, deadline)`: wake-up sources are (a) the nearest
`cooldown_until` among slots that are keyed, not benched, not in-flight, and
cooling (they become available by pure time passage), and (b) the caller deadline.
Return the smallest delta in seconds, or `None` when neither exists (then wait
solely on notification — a release, `add`, `clear_benched`). Spurious wakeups are
handled by the loop.

Notes:

- `policy.select` raising propagates as-is; nothing was mutated before the call, so
  no rollback is needed (the old re-enqueue-on-exception at `pool.py:253-256`
  disappears).
- `policy.select` returning `None` falls back to round-robin (`min(pick_seq)`,
  deterministic — preserves today's FIFO rotation for no-preference selection).
- `_partition_by_tier` keeps its logic (`pool.py:220-232`) but takes/returns slots
  and no longer needs the "others" list.
- Exploration/floor/rank stay inside `OptimizerPolicy` — untouched in this plan.

**Mutations** — all under `async with self._cond:`, with `notify_all()` where noted:

- `release(config)`: if the slot exists, `in_flight = False`; notify. A missing name
  is legal (removed mid-flight) — replaces the router's stale-slot check.
- `cool_down(config, delay)`: `cooldown_until = now + delay`, `fail_count += 1`,
  `in_flight = False`; persist to the state store exactly as today
  (`pool.py:291-297`, including `_store_cache = None` invalidation); notify.
  **No `call_later`.**
- `apply_shared_cooling(config)`: store-cache read unchanged (`pool.py:314-320`);
  on a stored COOLING entry copy `cooldown_until`, `fail_count = max(local, stored)`
  into the slot, `in_flight = False`, return True. No timer.
- `set_benched(name)`: set the flag only — no queue drain. An in-flight call
  finishes normally; the flag excludes the slot afterwards. `clear_benched(name)`:
  clear flag; notify.
- `add(cfg, key)`: upsert the slot, preserving `cooldown_until`/`fail_count`/
  `in_flight` of an existing one; `key=None` leaves a prior key intact (as today);
  notify. The keyless→keyed enqueue special-casing (`pool.py:102-109`) disappears.
- `drop(name)`: `self._slots.pop(name, None)` — the marker-cleanup loop
  (`pool.py:111-121`) collapses.
- `clear_cooling(name)` / `mark_quality_fail(name)`: slot field writes.

**Locking discipline** (getting this wrong causes lost wakeups or stalled
acquires — follow exactly):

- All slot access runs on the broker's event loop; plain field reads/writes need
  no lock. The Condition's lock exists only to make check-then-wait atomic
  against notify (asyncio requires the lock held for both `wait()` and
  `notify_all()`).
- `acquire`'s whole check/wait loop runs inside one `async with self._cond:` —
  the availability check and the `wait()` must not be separated by an `await`
  outside the lock, or a notify can slip between them.
- Every mutator that can create availability (`release`, `cool_down`, `add`,
  `clear_benched`, `drop`) does its slot-field changes and `notify_all()` inside
  a short `async with self._cond:` block.
- **Never await I/O while holding the lock.** `cool_down` mutates fields and
  notifies under the lock, then performs the state-store write after releasing it
  (the slot is already marked cooling, so no waiter can pick it meanwhile).
- The `asyncio.wait_for(self._cond.wait(), timeout)` idiom is safe on 3.11:
  a cancelled `Condition.wait()` re-acquires the lock before the timeout
  propagates, so the `async with` block stays consistent.

Trivial accessors (`__contains__`, `__len__`, `configs`, `config`, `has_key`,
`resolved_key`, `is_benched`, `is_deprecated`, `tier_of`, the demotion getters)
reroute mechanically to slot fields — no lock, no behavior change.

**Router** (`router.py`): delete lines 86-98 (stale-slot check + defensive keyless
branch); keep the eager zero-keyed check (line 74). Change the `except` at line 83
to `except TimeoutError` (acquire no longer raises `asyncio.QueueEmpty`).

**`pool.configs`**: keep the property; derive `{name: s.config for ...}` or retain a
parallel dict — callers (`broker.py`, `catalog.py`, `optimizer.py:351`) treat it as
a read-only mapping.

**Tests.**

- `tests/test_state.py` (21 tests over `InMemoryState`): delete; re-express the
  behavioral assertions (phase derivation, fail count) via `pool.state(name)` in
  `tests/test_pool.py`.
- `tests/test_pool.py` (48 tests): rewrite queue-internal tests against the public
  surface (`acquire`/`release`/`cool_down`/`state`). Behavioral assertions stay.
- Add: benched-while-in-flight (call finishes, slot excluded after); a waiter in
  `acquire(wait=None)` wakes when a cooldown expires, without any timer; `wait=0`
  raises immediately; finite `wait` times out at the deadline; round-robin order
  under `policy=None` (a-b-c-a-b-c); `policy.select` raising leaves the pool clean.
- `tests/test_router.py`, `test_broker*.py`, `test_optimizer*.py`, `test_sync*.py`:
  should pass unchanged except assertions on `asyncio.QueueEmpty` (→
  `TimeoutError`) or direct `InMemoryState` construction.

**Done when:** `invoke pre` + pytest green;
`grep -rn "call_later\|asyncio.Queue" src/llmbroker/broker/` returns nothing.

## 1.2 Configurable per-LLM concurrency (deliberate behavior change)

Today at most one request is in flight per LLM — a global serialization. Most
providers tolerate parallel requests fine; a few always 429 under concurrency. New
rule: **parallel calls to one LLM are allowed by default; a per-LLM cap opts into
serialization.**

- `RateLimit` (`models.py:264-270`) gains `parallel: int | None = None` — max
  simultaneous in-flight requests; `None` = unlimited, consistent with the other
  fields' "not enforced" semantics. Round-trip it through
  `LLMConfig.to_metadata`/`from_metadata` (`models.py:331+`) next to rpm/rpd/tpm/tpd.
  TOML: `rate_limit = { parallel = 1 }`.
- `_Slot.in_flight` becomes `int = 0`. Availability clause becomes:
  `cap is None or slot.in_flight < cap` where
  `cap = slot.config.rate_limit.parallel if slot.config.rate_limit else None`.
- `acquire` does `in_flight += 1`; `release` / `cool_down` /
  `apply_shared_cooling` do `in_flight = max(0, in_flight - 1)` (each caller holds
  exactly one).
- A 429 while siblings are in flight: the failing call cools the slot down (blocks
  new acquisitions); in-flight siblings finish normally and decrement on their own
  release/cool_down. Consecutive-fail backoff scaling applies per event, as today.
- Round-robin (`pick_seq`) and ranked selection are otherwise unchanged. Do not add
  load-based tie-breaking (YAGNI — 429/cooldown already handles overload).

**Tests:** two concurrent `acquire`s of a single-LLM pool both succeed by default;
with `parallel=1` the second blocks until release (i.e. today's behavior is exactly
the `parallel=1` configuration); cooldown set by one of two parallel calls blocks a
third acquire while the second call still completes and records normally;
metadata round-trip of `parallel` through a registry backend.

**Spec (same step):** update `architecture.md` "Core" bullet — replace the
one-in-flight/queue/`call_later` sentence with: "Parallel requests to one LLM are
allowed by default; `rate_limit.parallel` caps simultaneous in-flight requests per
LLM (1 = serialize). A cooling LLM is skipped until its cooldown expires."

## 1.3 Simplify `sync.py` construction

With no loop-bound queue created in `AsyncBroker.__init__`, the `Future` +
`call_soon_threadsafe(_build)` dance (`sync.py:114-137`) is unnecessary: construct
`AsyncBroker(...)` directly on the caller thread, then start the loop thread. The
`_run_loop`/`_shutdown`/`weakref.finalize` lifecycle stays. Delete the unused
`Future` import and the Queue-binding comment.

**Done when:** `tests/test_sync.py`, `tests/test_sync_info_logs.py` pass (minus any
test asserting the construction mechanism itself).

## Dropped steps (superseded by Plan 2)

Two steps from an earlier revision are intentionally absent: a shared `Debounce`
helper and the telemetry-wrapper-chain merge. Plan 2 (`simplify-learning.md`)
deletes the alert-debounce machinery and both wrapper classes outright, so
consolidating them first would be wasted work.

---

## Step order

1. **1.1** slot table (behavior-preserving; largest risk — lands alone)
2. **1.2** concurrency cap (the behavior change + spec update)
3. **1.3** sync.py

**Plan gate:** `invoke pre` + full `python -m pytest` green, zero skips. Then
proceed to `specs/plans/simplify-learning.md`.

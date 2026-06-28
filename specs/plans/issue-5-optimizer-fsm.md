# Plan: Optimizer FSM and adaptive delay tuning

Source of truth: https://github.com/andgineer/llmbroker/issues/5

## Current state

- `Optimizer` (optimizer.py:6–10) is a stub dataclass with only `judge_fraction`.
- `LifecyclePhase.OFFLINE` and `PROBING` already exist in `models.py:13–23` but are described as "P4 — never occur in P1".
- `InMemoryState` (state.py) tracks `_cooldown: dict[str, datetime]` and `_fail_count`; derives only AVAILABLE/COOLING.
- `LLMPool.cool_down()` (pool.py:105) uses `retry_after_seconds()` or falls back to `_DEFAULT_RATE_LIMIT_SEC = 60` — fixed, not learned.
- `Router._log_call()` (router.py:172) calls `telemetry.record(call)` — this is the interposition seam.
- `AsyncBroker.alerts()` (broker.py:194) is hardcoded to return `[]`.

## Architecture

### Optimizer as adaptive delay store

`Optimizer` gains parameters and per-LLM mutable delay state. No control loop runs in it directly — the delay is consulted by `LLMPool` at cool-down time and updated by `OptimizerTelemetry.record()`.

Add to `optimizer.py`:

```python
@dataclass
class Optimizer:
    judge_fraction: float = 0.0
    initial_delay: float = 60.0
    max_delay: float = 3600.0
    backoff_factor: float = 2.0
    decrease_factor: float = 0.75
    max_fail_count: int = 3      # consecutive rate-limit failures before OFFLINE
    offline_sleep: float = 300.0  # seconds to sleep before probing

    # mutable runtime state — not constructor args
    _current_delay: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _pending_alerts: list[Alert] = field(default_factory=list, init=False, repr=False)
    _rl_fail_count: dict[str, int] = field(default_factory=dict, init=False, repr=False)

    def delay_for(self, llm_name: str) -> float:
        return self._current_delay.get(llm_name, self.initial_delay)

    def rl_fail_count(self, llm_name: str) -> int:
        return self._rl_fail_count.get(llm_name, 0)

    def on_rate_limited(self, llm_name: str) -> float:
        """Increase delay; return new value (capped at max_delay)."""
        self._rl_fail_count[llm_name] = self._rl_fail_count.get(llm_name, 0) + 1
        d = min(self.delay_for(llm_name) * self.backoff_factor, self.max_delay)
        self._current_delay[llm_name] = d
        return d

    def on_success(self, llm_name: str) -> None:
        d = max(self.delay_for(llm_name) * self.decrease_factor, self.initial_delay)
        self._current_delay[llm_name] = d
        self._rl_fail_count[llm_name] = 0  # reset on success — standard circuit-breaker behaviour

    def on_probing_start(self, llm_name: str) -> None:
        """Prime rl_fail_count so one probe failure immediately re-triggers OFFLINE."""
        self._rl_fail_count[llm_name] = self.max_fail_count - 1

    def on_probing_success(self, llm_name: str) -> None:
        self._current_delay[llm_name] = self.initial_delay
        self._rl_fail_count[llm_name] = 0

    def seed_from_metrics(self, metrics: dict[str, LLMMetrics]) -> None:
        """Warm-start: prime delay hints from persisted metrics on restart."""
        for name, m in metrics.items():
            if m.last_status in (CallStatus.RATE_LIMITED, CallStatus.UNAVAILABLE):
                # Start at max to be conservative after a hot restart
                self._current_delay[name] = self.max_delay

    def alerts(self) -> list[Alert]:
        result = list(self._pending_alerts)
        self._pending_alerts.clear()
        return result

    def add_alert(self, msg: str) -> None:
        self._pending_alerts.append(Alert(message=msg))
```

Import `Alert`, `LLMMetrics`, `CallStatus` from `llmbroker.models` at module top.

### OptimizerTelemetry — the interposition decorator

New class in `optimizer.py` (not a separate file — keeps the module cohesive):

```python
class OptimizerTelemetry:
    """Wraps any TelemetryProtocol and drives Optimizer FSM from the live event stream."""

    def __init__(
        self,
        optimizer: Optimizer,
        inner: TelemetryProtocol,
        pool: LLMPool,
        *,
        on_go_offline: Callable[[str], None],
    ) -> None:
        self._opt = optimizer
        self._inner = inner
        self._pool = pool
        self._on_go_offline = on_go_offline

    async def record(self, call: Call) -> None:
        await self._inner.record(call)
        self._drive_fsm(call)

    async def record_quality(self, call_id: str, score: float) -> None:
        await self._inner.record_quality(call_id, score)

    def _drive_fsm(self, call: Call) -> None:
        name = call.llm_name
        if call.status in (CallStatus.RATE_LIMITED, CallStatus.UNAVAILABLE):
            self._opt.on_rate_limited(name)
            if self._opt.rl_fail_count(name) >= self._opt.max_fail_count:
                self._pool.set_offline(name)
                self._opt.add_alert(f"{name} went OFFLINE after {self._opt.rl_fail_count(name)} failures")
                self._on_go_offline(name)
        elif call.status == CallStatus.OK:
            phase = self._pool.state(name).phase
            if phase == LifecyclePhase.PROBING:
                self._opt.on_probing_success(name)
                self._pool.set_available(name)
            else:
                self._opt.on_success(name)
```

`OptimizerTelemetry` also proxies all `QueryableTelemetryProtocol` methods when the inner telemetry supports them, so `isinstance` checks continue to work. The cleanest way: implement the queryable methods and delegate, and declare the class conditionally. Simplest approach: always forward attribute lookups for unknown attributes to `self._inner` via `__getattr__`.

### FSM state extension in InMemoryState

Add `_phase_override: dict[str, LifecyclePhase]` (for OFFLINE and PROBING only).

Change `get_state()`:
```python
def get_state(self, name: str) -> LLMState:
    override = self._phase_override.get(name)
    if override in (LifecyclePhase.OFFLINE, LifecyclePhase.PROBING):
        return LLMState(
            phase=override,
            cooldown_until=self._cooldown.get(name),
            fail_count=self._fail_count.get(name, 0),
        )
    # existing AVAILABLE / COOLING derivation ...
```

Add `set_phase_override(name, phase)` and `clear_phase_override(name)` methods.

`LLMPool.set_available(name)` calls both `_state.clear_phase_override(name)` and `_state.clear_cooling(name)`. The cooldown must be cleared explicitly — if the cooldown timestamp hasn't expired yet, `get_state()` would otherwise return COOLING instead of AVAILABLE for the recovered LLM. Rate-limit fail count is tracked in `Optimizer._rl_fail_count` and is reset by `on_probing_success`, not here.

`LLMPool.set_probing(name)` sets the phase override to PROBING only.

### LLMPool changes

1. Add `set_offline(name)`, `set_probing(name)`, `set_available(name)` — thin wrappers. `set_available` calls both `_state.clear_phase_override(name)` and `_state.clear_cooling(name)`.

2. Change `cool_down()` signature to accept an optional `delay_override: float | None = None`. When provided, use it instead of `retry_after_seconds()`. The router calls this with the optimizer's delay:

   ```python
   delay = optimizer.delay_for(config.name) if optimizer else None
   await self._pool.cool_down(config, exc.response.headers, delay_override=delay)
   ```

   `cool_down()` resolves: `delay = delay_override if delay_override is not None else retry_after_seconds(headers, _DEFAULT_RATE_LIMIT_SEC)`.

   `Router.__init__` gains `optimizer: Optimizer | None = None`; the router uses it only to read `delay_for()` before calling `pool.cool_down()`. Add `from llmbroker.optimizer import Optimizer` to `router.py` imports.

3. `cool_down()` is the only place `_fail_count[name]` is incremented (for shared-state tracking) — it does so before scheduling the re-queue timer. `cool_down()` always schedules re-queue via `call_later`; OFFLINE is decided exclusively by `_drive_fsm`, not here.

4. Replace `loop.call_later(float(delay), self._queue.put_nowait, config)` with a guarded closure:

   ```python
   def _reenqueue() -> None:
       if self._state.get_state(config.name).phase not in (LifecyclePhase.OFFLINE, LifecyclePhase.PROBING):
           self._queue.put_nowait(config)
   loop.call_later(float(delay), _reenqueue)
   ```

   This prevents the timer from re-adding a slot that `_drive_fsm` already took offline between the time `cool_down()` scheduled the timer and the time it fires.

### Probing background task

Probing is **passive**: `_probe_loop` adds one slot back to the pool with phase PROBING and waits for the next real incoming request to act as the probe. No synthetic request is sent. This is intentional — if there is no traffic, recovery is not needed. Active probing would add complexity (template request, model, prompt) for an edge-case that does not matter under real load.

`AsyncBroker` starts one probing task per LLM when it goes OFFLINE. Task lifecycle:

```python
async def _probe_loop(self, llm_name: str) -> None:
    while True:
        await asyncio.sleep(self._optimizer.offline_sleep)
        self._pool.set_probing(llm_name)
        self._optimizer.on_probing_start(llm_name)
        config = self._pool.config(llm_name)
        self._pool.release(config)  # adds exactly one slot; asyncio semaphore ensures one caller gets it
        # Router will pick it up; OptimizerTelemetry.record() handles the outcome.
        # If the probe fails, _drive_fsm() calls on_go_offline again → restarts this loop.
        break  # one shot per invocation; on_go_offline re-spawns if needed
```

`on_go_offline` callback (passed to `OptimizerTelemetry.__init__`) is:
```python
def _on_go_offline(self, llm_name: str) -> None:
    if any(t.get_name() == f"probe-{llm_name}" and not t.done() for t in self._bg_tasks):
        return  # probe already running for this LLM
    task = asyncio.create_task(self._probe_loop(llm_name), name=f"probe-{llm_name}")
    self._bg_tasks.add(task)
    task.add_done_callback(self._bg_tasks.discard)
```

`self._bg_tasks` is a `set[asyncio.Task]`, not a `list`. Change the declaration in `__init__` from `self._bg_tasks: list[asyncio.Task] = []` to `self._bg_tasks: set[asyncio.Task] = set()`. The guard above uses `t.get_name()` to prevent duplicate probe tasks if two concurrent rate-limit failures both trigger `on_go_offline` for the same LLM before either `_drive_fsm` runs.

Update `aclose()` to snapshot the set before iterating — `for task in list(self._bg_tasks):` — so that done callbacks (`discard`) firing mid-`await` do not raise `RuntimeError: Set changed size during iteration`.

### Wiring in AsyncBroker

In `__init__`, after constructing `self._router`:

```python
self._base_telemetry = telemetry  # kept before wrapping; used for isinstance checks
if self._optimizer is not None:
    effective_telemetry = OptimizerTelemetry(
        self._optimizer,
        telemetry,
        pool,
        on_go_offline=self._on_go_offline,
    )
    self._router = Router(pool, effective_telemetry, optimizer=self._optimizer, user_id=user_id)
    self._pool_view = PoolView(pool, effective_telemetry, user_id=user_id)
    self._telemetry = effective_telemetry
```

In `ensure_pool()`, after `await self._catalog.provision()`, add warm-start:

```python
if self._optimizer is not None and isinstance(self._base_telemetry, QueryableTelemetryProtocol):
    metrics = await self._telemetry.metrics()
    self._optimizer.seed_from_metrics(metrics)
```

`self._base_telemetry` (not `self._telemetry`) is used for the isinstance check because `OptimizerTelemetry` always defines `metrics/calls/purge_calls` as delegation stubs, making `isinstance(self._telemetry, QueryableTelemetryProtocol)` always `True` regardless of whether the inner backend is queryable. Checking the unwrapped original avoids a runtime `AttributeError` when the inner is non-queryable.

Implement `alerts()`:
```python
async def alerts(self) -> list[Alert]:
    if self._optimizer is None:
        return []
    return self._optimizer.alerts()
```

### QueryableTelemetryProtocol proxy

`OptimizerTelemetry` must not break the `isinstance(telemetry, QueryableTelemetryProtocol)` check when the inner backend is queryable. Add `__getattr__` forwarding and explicitly list the queryable methods as delegates:

```python
async def metrics(self, *, since=None, user_id=None):
    return await self._inner.metrics(since=since, user_id=user_id)

async def calls(self, *, limit, user_id=None):
    return await self._inner.calls(limit=limit, user_id=user_id)

async def purge_calls(self, *, before):
    return await self._inner.purge_calls(before=before)
```

Since `QueryableTelemetryProtocol` is `@runtime_checkable`, structural matching works — these three methods being present is enough for `isinstance` to return `True`. This is intentional: `OptimizerTelemetry` always exposes the queryable surface so callers (other than the warm-start check) can use it uniformly. The warm-start check in `ensure_pool` is the one place that must bypass the wrapper and check `self._base_telemetry` directly (see Wiring section).

### Rolling per-(llm, operation) aggregates

The issue mentions rolling aggregates per `(llm, operation)`. Phase 5 (the judge fraction feature) needs these; the FSM only needs per-LLM fail counts and delay, which are already tracked. Keep rolling aggregates as a stub field on `Optimizer`:

```python
_rolling: dict[tuple[str, str | None], collections.deque] = field(default_factory=dict, init=False, repr=False)
```

Populate in `_drive_fsm`, but leave unused for now. Use `field()` (consistent with `_current_delay`, `_pending_alerts`, `_rl_fail_count`), not `__post_init__`.

`operation` is sourced from `Call.operation` (already present in `models.py`). The aggregate key is `(llm_name, None)` for calls where `operation` was not supplied.

## Files changed

| File | Change |
|---|---|
| `src/llmbroker/optimizer.py` | Expand stub: `Optimizer` params + runtime state (`_current_delay`, `_pending_alerts`, `_rl_fail_count`, `_rolling`); add `rl_fail_count`, `on_probing_start`; `OptimizerTelemetry`; `_drive_fsm` |
| `src/llmbroker/broker/state.py` | Add `_phase_override`; `set_phase_override` / `clear_phase_override`; update `get_state` |
| `src/llmbroker/broker/pool.py` | Add `set_offline` / `set_probing` / `set_available` (clears phase override + cooldown); `delay_override` + fail_count increment in `cool_down`; replace bare `put_nowait` callback with `_reenqueue` closure |
| `src/llmbroker/broker/router.py` | Add `from llmbroker.optimizer import Optimizer`; gain `optimizer: Optimizer | None = None` in `__init__`; pass `delay_for()` as `delay_override` to `pool.cool_down()` |
| `src/llmbroker/broker/broker.py` | Store `_base_telemetry`; wire `OptimizerTelemetry` + pass `optimizer` to `Router`; warm-start via `_base_telemetry`; change `_bg_tasks` to `set[asyncio.Task]`; `_on_go_offline` with duplicate-guard + done callback; `_probe_loop` with `on_probing_start`; snapshot `_bg_tasks` in `aclose()`; non-empty `alerts()` |
| `src/llmbroker/models.py` | No changes needed — `Call.operation` and OFFLINE/PROBING phases already present |

## Tests to add

- `tests/test_optimizer.py`:
  - `test_delay_increases_on_rate_limit` — repeated calls to `on_rate_limited` cap at `max_delay`
  - `test_delay_decreases_on_success` — `on_success` reduces toward `initial_delay`
  - `test_probing_success_resets_delay` — `on_probing_success` sets `initial_delay`
  - `test_seed_from_metrics_conservative` — `seed_from_metrics` sets `max_delay` for recently failed LLMs
  - `test_fsm_available_to_cooling` — RATE_LIMITED record → `pool.state().phase == COOLING`
  - `test_fsm_cooling_to_offline` — repeated failures hit `max_fail_count` → `pool.state().phase == OFFLINE`
  - `test_fsm_probing_to_available` — OK record on PROBING LLM → `pool.state().phase == AVAILABLE`
  - `test_fsm_probing_failure_to_offline` — RATE_LIMITED record on PROBING LLM after `on_probing_start` sets `_rl_fail_count` to `max_fail_count - 1` → back to OFFLINE after one failure
  - `test_cold_boot_no_telemetry` — `AsyncBroker` with `NoTelemetry()` and `optimize=True` doesn't crash
  - `test_warm_start_activates_with_queryable` — queryable backend causes `seed_from_metrics` to be called
  - `test_alerts_returns_offline_llm` — OFFLINE transition produces alert in `broker.alerts()`
  - `test_optimizer_telemetry_proxies_queryable` — `isinstance(opt_telemetry, QueryableTelemetryProtocol)` is True when inner is queryable

## Verification

```
invoke pre
python -m pytest
```

# Plan: Pool hygiene and human-facing alerts

Source of truth: https://github.com/andgineer/llmbroker/issues/7

## Current state

What already exists:
- `AsyncBroker.alerts()` is implemented and returns `[]` when `optimize=False` ✓
- `Alert` model exists in `models.py` ✓
- `Optimizer._pending_alerts`, `add_alert()`, and `alerts()` are wired ✓
- `_drive_fsm` in `OptimizerTelemetry` reacts to `RATE_LIMITED`/`UNAVAILABLE`/`OK`/`ERROR` ✓

What is wrong or missing:
1. **Noise alert on OFFLINE**: `_drive_fsm` currently calls `opt.add_alert()` when an LLM goes OFFLINE
   after rate-limit backoff. This is auto-recoverable (probe loop handles it) — not human-actionable.
2. **No "API key dead" detection**: 401/403 responses flow through as generic `ERROR` with `http_status`
   set, but `_drive_fsm` ignores `http_status` entirely.
3. **No "pool under-provisioned" alert**: no detection when all LLMs are simultaneously OFFLINE/COOLING.
4. **No pool hygiene / retirement**: no mechanism to permanently retire a chronically-failing LLM after
   repeated failed probe cycles.

## Architecture

### Remove the noise OFFLINE alert

In `_drive_fsm`, delete the `opt.add_alert(f"{name} went OFFLINE …")` line. Going OFFLINE is
auto-recoverable — the probe loop re-adds the slot after `offline_sleep`. It is not a signal
that requires human action.

The quality-floor fallback alert in `OptimizerPolicy` is internal noise too, but it is already
filtered by `optimize=False` semantics — leave it unchanged.

### `Optimizer`: probe cycle tracking

Add to the `Optimizer` dataclass:

```python
max_probe_cycles: int = 5

_probe_cycles: dict[str, int] = field(default_factory=dict, init=False, repr=False)
```

Add two methods:

```python
def increment_probe_cycles(self, llm_name: str) -> int:
    count = self._probe_cycles.get(llm_name, 0) + 1
    self._probe_cycles[llm_name] = count
    return count

def probe_cycles(self, llm_name: str) -> int:
    return self._probe_cycles.get(llm_name, 0)

def reset_probe_cycles(self, llm_name: str) -> None:
    self._probe_cycles.pop(llm_name, None)
```

Update `on_probing_success` to reset the counter on recovery:

```python
def on_probing_success(self, llm_name: str) -> None:
    self._current_delay[llm_name] = self.initial_delay
    self._rl_fail_count[llm_name] = 0
    self._probe_cycles.pop(llm_name, None)
```

### `_drive_fsm`: auth-failure and retirement

Replace the current `ERROR` branch (which only acts on PROBING phase) with:

```python
elif call.status == CallStatus.ERROR:
    if call.http_status in (401, 403):
        # API key is dead — human must fix; no auto-recovery probe.
        cfg = self._pool.configs.get(name)
        ref = cfg.api_key_ref if cfg else "unknown"
        self._pool.drop(name)
        self._opt.add_alert(
            f"{name}: API key appears dead (HTTP {call.http_status})"
            f" — check api_key_ref '{ref}'"
        )
    elif self._pool.state(name).phase is LifecyclePhase.PROBING:
        cycles = self._opt.increment_probe_cycles(name)
        if cycles >= self._opt.max_probe_cycles:
            self._pool.drop(name)
            self._opt.add_alert(
                f"{name}: retired after {cycles} failed probe cycles"
                " — remove from registry or fix connectivity"
            )
        else:
            self._pool.set_offline(name)
            self._on_go_offline(name)
```

**Why `pool.drop()` for both cases:**

After an `_attempt` call in the router, `pool.release(config)` is always called *before*
`record()` triggers `_drive_fsm`. So by the time `_drive_fsm` runs, the stale slot is
already back in the queue. Calling `pool.drop(name)` removes the LLM from `_configs`; the
next `acquire()` cycle dequeues the stale slot, hits the existing
`if config.name not in self._pool: continue` guard, and discards it cleanly.

For auth failure: no probe task is started. If the key is later fixed, the operator
must call `broker.add(cfg)` to re-add the LLM.

For retirement: no new probe task is started either. Any previous probe task checks
`llm_name not in self._pool` on wake-up and exits early (existing guard in `_probe_loop`).

### `AsyncBroker`: "pool under-provisioned" alert

Add two new private attributes after `self._bg_tasks`:

```python
_last_underprov_alert: float = 0.0
_underprov_alert_interval: float = 60.0
```

Extract the current `ask()`/`chat()` body into a helper that wraps `NoLLMAvailableError`:

```python
def _maybe_alert_underprov(self) -> None:
    if self._optimizer is None:
        return
    if not self._pool.configs:
        return
    now = time.monotonic()
    if now - self._last_underprov_alert < self._underprov_alert_interval:
        return
    all_offline = all(
        self._pool.state(name).phase is not LifecyclePhase.AVAILABLE
        for name in self._pool.configs
    )
    if all_offline:
        self._last_underprov_alert = now
        self._optimizer.add_alert(
            "pool under-provisioned: all LLMs are OFFLINE or COOLING"
            " — add more LLMs to the registry"
        )
        # Note: the condition `phase is not AVAILABLE` also fires when all LLMs are
        # in PROBING phase. The alert text ("OFFLINE or COOLING") doesn't mention
        # PROBING, but this is acceptable — a pool where every member is simultaneously
        # probing is equally under-provisioned. The wording is intentionally human-friendly
        # rather than exhaustively precise.
```

In both `ask()` and `chat()`, catch `NoLLMAvailableError`, call `_maybe_alert_underprov()`,
then re-raise:

```python
async def chat(self, messages, *, ...) -> AsyncResult:
    await self.ensure_pool()
    try:
        return await self._router.chat(messages, ...)
    except NoLLMAvailableError:
        self._maybe_alert_underprov()
        raise
```

Add `import time` to `broker.py`. Import `NoLLMAvailableError` from `llmbroker.exceptions`.

## Files changed

| File | Change |
|---|---|
| `src/llmbroker/optimizer.py` | `Optimizer`: add `max_probe_cycles`, `_probe_cycles`, `increment_probe_cycles()`, `probe_cycles()`, `reset_probe_cycles()`. Update `on_probing_success` to reset cycles. `_drive_fsm`: remove noise OFFLINE alert; add 401/403 branch (`pool.drop` + alert); update ERROR/PROBING branch to count cycles and retire at `max_probe_cycles` |
| `src/llmbroker/broker/broker.py` | Add `_last_underprov_alert`, `_underprov_alert_interval`. Add `_maybe_alert_underprov()` (plain `def`, no I/O). Wrap `ask()` and `chat()` router calls to catch `NoLLMAvailableError`, call `_maybe_alert_underprov()`, re-raise. In `add()`, call `self._optimizer.reset_probe_cycles(cfg.name)` when optimizer is set. Add `import time` and import `NoLLMAvailableError` |

## Tests to add (`tests/test_optimizer.py`)

- `test_no_offline_alert_on_rate_limit_backoff` — max `rl_fail_count` reached → LLM goes OFFLINE but `opt.alerts()` returns `[]` (no human alert for auto-recoverable event)
- `test_auth_failure_401_drops_llm_and_alerts` — `_drive_fsm` called with `ERROR/http_status=401` → `pool.drop(name)` called; alert message contains "API key" and api_key_ref
- `test_auth_failure_403_drops_llm_and_alerts` — same for 403
- `test_drop_removes_config_but_not_queue_slot` — after drop, `acquire()` dequeues the stale slot and the name is no longer in pool. Note: this test verifies the pool-side precondition only; the actual router guard (`if config.name not in self._pool: continue`) is pre-existing code and not exercised here. A full router integration test was judged disproportionate for a pre-existing guard.
- `test_auth_failure_during_probing_phase` — 401 while in PROBING phase: `pool.drop()` called, `_on_go_offline` NOT called again (no duplicate probe task), and `probe_cycles()` is irrelevant (counter not incremented)
- `test_probe_failure_increments_cycle_count` — single probe ERROR → `opt.probe_cycles(name) == 1`; LLM goes OFFLINE, probe loop restarted; no human alert
- `test_probe_success_resets_cycle_count` — probe cycles incremented then probe OK → `opt.probe_cycles(name) == 0`; no retirement alert
- `test_retirement_after_max_probe_cycles` — `max_probe_cycles=2`; two consecutive probe ERRORs → `pool.drop(name)` called; alert contains "retired"; third `acquire()` skips stale slot
- `test_no_retirement_before_max_probe_cycles` — `max_probe_cycles=3`; two probe ERRORs → LLM NOT dropped; still in `pool.configs`. Note: the unit-test mock appends to `offline_calls` twice (both ERRORs are below threshold, so both trigger `set_offline`+`_on_go_offline`). In the real `AsyncBroker`, the second `_on_go_offline` is silently deduplicated by the existing probe-task guard — no duplicate probe task is started. The mock intentionally does not replicate this guard.
- `test_underprovisioned_alert_when_all_offline` — broker catches `NoLLMAvailableError`, all pool members OFFLINE → `broker.alerts()` returns alert with "under-provisioned"
- `test_no_underprov_alert_when_some_available` — same but one LLM is AVAILABLE → no alert
- `test_no_underprov_alert_when_optimize_false` — `optimize=False` → no alert even if all OFFLINE
- `test_underprov_alert_debounced` — two consecutive `NoLLMAvailableError` within interval → only one alert in `pending_alerts`
- `test_underprov_alert_via_ask_wiring` — integration test: drains queue, sets LLM offline, calls `broker.ask(wait=0)` → `NoLLMAvailableError` flows through the `try/except` in `ask()` → `_maybe_alert_underprov()` fires → alert appears in `broker.alerts()`. Guards the `try/except` wiring in `ask()` and `chat()` (the direct `_maybe_alert_underprov()` tests do not cover this path).
- `test_alerts_returns_empty_optimize_false` — `AsyncBroker(optimize=False).alerts()` returns `[]` (regression guard)

## Verification

```
invoke pre
python -m pytest
```

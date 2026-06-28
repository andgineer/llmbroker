# Plan: Optimizer tiered selection policy

Source of truth: https://github.com/andgineer/llmbroker/issues/6

## Current state

- `Router.chat()` calls `pool.acquire(wait)` which dequeues the next `LLMConfig` from
  a plain `asyncio.Queue` — pure FIFO, i.e. round-robin.
- `Optimizer._rolling` is a stub: `_touch_rolling()` creates empty deques but never
  appends calls; no stats methods exist.
- `Optimizer` has no selection-related parameters.
- `LLMConfig` has no `max_tpm` field.
- No `SelectionPolicy` protocol or implementations exist.

## Architecture

### SelectionPolicy protocol

Add to `optimizer.py` (not a new file — keeps the selection concern with the optimizer):

```python
class SelectionPolicy(Protocol):
    def select(self, candidates: list[LLMConfig], *, operation: str | None) -> LLMConfig | None:
        """Pick the best candidate from the currently available list.
        None means no preference — caller may pick any."""
```

`candidates` is the set of LLMs whose slots are currently free (phase AVAILABLE). The pool
handles availability gating before calling this; the policy only sees the gated set.

### RoundRobinPolicy

```python
class RoundRobinPolicy:
    def select(self, candidates: list[LLMConfig], *, operation: str | None) -> LLMConfig | None:
        return candidates[0] if candidates else None
```

Default when no optimizer is present. Preserves existing semantics exactly.

### New Optimizer parameters and runtime state

Add to the `Optimizer` dataclass constructor args:

```python
min_sample_count: int = 10            # below this count, don't apply quality floor or ranking
usable_rate_floor: float = 0.5        # Tier-2 gate: drop candidates below this per-operation rate
exploration_fraction: float = 0.1     # fraction of selections routed uniformly at random
rolling_window: int = 50              # max entries per (llm_name, operation) rolling deque
tpm_window_sec: float = 60.0          # sliding-window width for TPM tracking
background_operations: frozenset[str] = field(default_factory=frozenset)
                                      # operations ranked by quality; else ranked by latency
```

Add new mutable runtime fields:

```python
_tpm_windows: dict[str, collections.deque] = field(default_factory=dict, init=False, repr=False)
# per-llm deque of (monotonic_time: float, tokens: int)
```

### Rolling-aggregate maintenance

Replace `_touch_rolling()` (currently creates empty deques, never appended) with
`_record_rolling(llm_name, operation, call)`:

```python
def _record_rolling(self, llm_name: str, operation: str | None, call: Call) -> None:
    key: tuple[str, str | None] = (llm_name, operation)
    if key not in self._rolling:
        self._rolling[key] = collections.deque(maxlen=self.rolling_window)
    self._rolling[key].append(call)
    if call.status == CallStatus.OK and call.usage and call.usage.total_tokens:
        self._record_tpm(llm_name, call.usage.total_tokens)
```

Update `_drive_fsm` (in `OptimizerTelemetry`) to call `self._opt._record_rolling(name, call.operation, call)  # noqa: SLF001` instead of `self._opt._touch_rolling(name, call.operation)  # noqa: SLF001`.

### Stats methods on Optimizer

```python
def _samples(self, llm_name: str, operation: str | None) -> collections.deque:
    return self._rolling.get((llm_name, operation), collections.deque())

def usable_rate(self, llm_name: str, operation: str | None) -> float | None:
    """Bayesian (Laplace-smoothed) rate. Returns None if fewer than min_sample_count."""
    s = self._samples(llm_name, operation)
    if len(s) < self.min_sample_count:
        return None
    ok = sum(1 for c in s if c.status == CallStatus.OK)
    return (ok + 1) / (len(s) + 2)   # Beta(1,1) prior

def mean_latency_ms(self, llm_name: str, operation: str | None) -> float | None:
    """Mean latency of OK calls; None if no OK call recorded."""
    vals = [c.latency_ms for c in self._samples(llm_name, operation)
            if c.status == CallStatus.OK and c.latency_ms is not None]
    return sum(vals) / len(vals) if vals else None

def _record_tpm(self, llm_name: str, tokens: int) -> None:
    if llm_name not in self._tpm_windows:
        self._tpm_windows[llm_name] = collections.deque()
    self._tpm_windows[llm_name].append((time.monotonic(), tokens))
    # Deque is unbounded by count; old entries are evicted lazily in tpm_used().
    # This is intentional: TPM bounding is time-based, not count-based.

def tpm_used(self, llm_name: str) -> int:
    """Total tokens sent to llm_name in the last tpm_window_sec."""
    window = self._tpm_windows.get(llm_name)
    if not window:
        return 0
    cutoff = time.monotonic() - self.tpm_window_sec
    while window and window[0][0] < cutoff:
        window.popleft()
    return sum(t for _, t in window)
```

Add `import time` at top of `optimizer.py`.

### LLMConfig: max_tpm field

Add to `LLMConfig` in `models.py`:

```python
max_tpm: int | None = None
```

When `None`: TPM is not a ranking factor for that LLM (treated as unlimited). When set,
an LLM with `tpm_used >= max_tpm` is given the lowest TPM-headroom rank score.

### OptimizerPolicy

Add to `optimizer.py`:

```python
class OptimizerPolicy:
    def __init__(self, optimizer: Optimizer) -> None:
        self._opt = optimizer

    def select(self, candidates: list[LLMConfig], *, operation: str | None) -> LLMConfig | None:
        if not candidates:
            return None
        # Exploration reserve: bypass ranking so low-ranked LLMs keep accumulating data.
        if random.random() < self._opt.exploration_fraction:
            return random.choice(candidates)
        # Tier 2: quality floor gate.
        gated = [c for c in candidates if self._passes_floor(c, operation)]
        pool = gated if gated else candidates   # never starve; add alert when floor drops all
        if not gated:
            self._opt.add_alert(
                f"quality floor {self._opt.usable_rate_floor} dropped all candidates "
                f"for operation={operation!r}; falling back to round-robin"
            )
        # Tier 3+4: objective ranking.
        is_background = operation in self._opt.background_operations
        return min(pool, key=lambda c: self._rank_key(c, operation, is_background))

    def _passes_floor(self, config: LLMConfig, operation: str | None) -> bool:
        rate = self._opt.usable_rate(config.name, operation)
        return rate is None or rate >= self._opt.usable_rate_floor
        # rate is None → not enough samples → never filtered out

    def _rank_key(
        self, config: LLMConfig, operation: str | None, is_background: bool
    ) -> tuple:
        rate_val = self._opt.usable_rate(config.name, operation)
        rate = rate_val if rate_val is not None else 0.5
        latency_val = self._opt.mean_latency_ms(config.name, operation)
        latency = latency_val if latency_val is not None else float("inf")
        max_tpm = config.max_tpm
        if max_tpm is not None:
            tpm_headroom = max(0, max_tpm - self._opt.tpm_used(config.name))
            tpm_sort = -tpm_headroom  # prefer more headroom (ascending sort key)
        else:
            tpm_sort = 0             # unlimited — no preference
        if is_background:
            # Quality DESC, latency ASC, tpm_headroom DESC
            return (-rate, latency, tpm_sort)
        else:
            # Latency ASC, quality DESC, tpm_headroom DESC
            return (latency, -rate, tpm_sort)
```

Add `import random` at top of `optimizer.py`.

### LLMPool.acquire — drain-and-pick

Change `acquire()` signature:

```python
async def acquire(
    self,
    wait: float | None,
    *,
    policy: SelectionPolicy | None = None,
    operation: str | None = None,
) -> LLMConfig:
```

Implementation:

```python
async def acquire(self, wait, *, policy=None, operation=None):
    first = await self._queue_acquire(wait)   # existing wait logic, extracted
    if policy is None:
        return first
    # Drain all immediately available slots (no await — safe: single event loop).
    available = [first]
    while True:
        try:
            available.append(self._queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    picked = policy.select(available, operation=operation)
    if picked is None:
        picked = available[0]
    for cfg in available:
        if cfg is not picked:
            self._queue.put_nowait(cfg)
    return picked
```

Extract the current wait-based dequeue into a private `_queue_acquire(wait)`:

```python
async def _queue_acquire(self, wait: float | None) -> LLMConfig:
    if wait is None:
        return await self._queue.get()
    if wait == 0:
        return self._queue.get_nowait()
    return await asyncio.wait_for(self._queue.get(), timeout=wait)
```

The drain+put-back block has no `await`, so it is atomic with respect to the event loop:
no other coroutine can interleave between the drain and the put-back.

### Router changes

`Router.__init__` gains `policy: SelectionPolicy | None = None` (already has `optimizer`).
Pass it to `pool.acquire`:

```python
config = await self._pool.acquire(wait, policy=self._policy, operation=operation)
```

Store as `self._policy = policy`.

### AsyncBroker wiring

After constructing `OptimizerTelemetry`, also create an `OptimizerPolicy`:

```python
if self._optimizer is not None:
    effective_telemetry = OptimizerTelemetry(...)
    policy: SelectionPolicy | None = OptimizerPolicy(self._optimizer)
else:
    effective_telemetry = telemetry
    policy = None

self._router = Router(pool, effective_telemetry, user_id=user_id,
                      optimizer=self._optimizer, policy=policy)
```

No public API change to `AsyncBroker.__init__` — `Optimizer` can be constructed
with `background_operations` to configure the per-operation objective:

```python
broker = AsyncBroker(
    registry,
    optimize=Optimizer(background_operations=frozenset({"batch-summarize"})),
)
```

## Files changed

| File | Change |
|---|---|
| `src/llmbroker/models.py` | Add `max_tpm: int | None = None` to `LLMConfig` |
| `src/llmbroker/optimizer.py` | Add `SelectionPolicy` protocol, `RoundRobinPolicy`, `OptimizerPolicy`; add config params (`min_sample_count`, `usable_rate_floor`, `exploration_fraction`, `rolling_window`, `tpm_window_sec`, `background_operations`) and runtime field `_tpm_windows` to `Optimizer`; replace `_touch_rolling` with `_record_rolling`; add stats methods (`usable_rate`, `mean_latency_ms`, `tpm_used`, `_record_tpm`); add `import time`, `import random`; update `_drive_fsm` to call `_record_rolling` |
| `src/llmbroker/broker/pool.py` | Extract `_queue_acquire`; extend `acquire` to accept `policy` + `operation` and implement drain-and-pick |
| `src/llmbroker/broker/router.py` | Accept and store `policy`; pass `policy` + `operation` to `pool.acquire` |
| `src/llmbroker/broker/broker.py` | Create `OptimizerPolicy` when optimizer is set, else `policy=None`; pass `policy` to `Router` |

## Tests to add (`tests/test_optimizer.py`, new test module)

- `test_record_rolling_populates_deque` — `_record_rolling` appends calls; deque respects `rolling_window` maxlen
- `test_usable_rate_none_below_min_sample_count` — returns None when < `min_sample_count` entries
- `test_usable_rate_laplace_smoothed` — 5/10 successes → (5+1)/(10+2) ≈ 0.5
- `test_mean_latency_ignores_non_ok` — only OK calls count
- `test_tpm_used_sliding_window` — entries older than `tpm_window_sec` are evicted
- `test_round_robin_policy_picks_first` — `RoundRobinPolicy.select([a, b, c])` returns `a`
- `test_optimizer_policy_quality_floor_gates` — LLM below floor is excluded; falls back to all when all fail floor
- `test_optimizer_policy_background_ranks_by_quality` — operation in `background_operations` → higher-rate LLM ranked first
- `test_optimizer_policy_interactive_ranks_by_latency` — operation not in set → lower-latency LLM ranked first
- `test_optimizer_policy_exploration_bypasses_ranking` — with `exploration_fraction=1.0`, selection is random (not always the best)
- `test_optimizer_policy_no_data_does_not_filter` — LLM with 0 samples passes quality floor
- `test_pool_acquire_drain_and_pick` — multiple available slots; policy picks non-first; others returned to queue
- `test_pool_acquire_single_slot_no_drain` — single slot; policy receives list of one; no put_nowait called
- `test_router_passes_policy_to_acquire` — Router passes its policy and operation through to `pool.acquire`
- `test_broker_creates_optimizer_policy_when_optimizer_set` — `AsyncBroker(optimize=True)` wires `OptimizerPolicy`
- `test_broker_round_robin_when_no_optimizer` — `AsyncBroker(optimize=False)` passes `policy=None` to `Router`; `pool.acquire` returns the first available slot without drain-and-pick
- `test_tpm_headroom_respected_in_rank_key` — LLM with more TPM headroom ranked first when quality/latency tied
- `test_optimizer_policy_unknown_latency_ranked_last_interactive` — LLM with no OK calls gets `latency=inf` and loses to any tested LLM in interactive mode (not ranked first as it would with `or 0.0`)

## Verification

```
invoke pre
python -m pytest
```

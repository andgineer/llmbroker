# Mission-conformance fixes

Goal: close every gap found between the code and `specs/reference/mission.md`.
Confirmed bugs first, then behavior decisions, then simplifications, then new
integration tests that pin the mission claims.

## Locked decisions (from the maintainer — do not re-litigate)

1. **Client-side 4xx (400/404/422… — every 4xx except 429, 401, 403):** fail
   over to the next model **without any cooldown**. The failing model is
   excluded for the rest of the current request only.
2. **`wait` is taken literally.** `wait=None` means "wait as long as needed" —
   never converted to an error while at least one model can still come back by
   itself (cooling). `wait=N` is honored as given.
3. **`wait=N` bounds the whole `ask()`/`chat()` call**, not each internal
   acquire attempt (follows from "do literally what the caller asked").
4. **When nothing can ever become available** (pool empty, every model
   keyless, every model disabled, every model dropped for a dead key) —
   raise immediately even under `wait=None`: there is no event that would
   wake the waiter. This is the fix for the confirmed hang (see Bug A).
5. **One exception:** `NoLLMAvailableError` with a `reason` attribute;
   `AllLLMsFailedError` is deleted from the public API (0.x, single known
   installation — breaking change accepted).
6. **`broker.calls()` stays scoped** to the broker's own scope. In multi-user
   mode a user-scoped broker must not see other users' journal rows. An admin
   panel reads the store backend directly (`QueryableStoreProtocol`) — that is
   already the documented supported path; no code change.
7. Never bump the version; never call ruff directly — `invoke pre` only.

## Confirmed bugs (reproduced)

**Bug A — dead key hangs `ask()` forever.** One model in the pool, provider
returns 401, default `wait=None`: the learning hook drops the slot, then
`LLMPool.acquire(None)` blocks on the condition with `_wake_timeout` returning
`None` — nothing will ever notify. Reproduced with a script; `ask()` never
returns.

**Bug B — dead-key drop is resurrected by the rebuild.** In
`_LearningHook.maybe_rebuild` (`src/llmbroker/broker/learning.py:123-141`) the
order is: apply scores → `_apply_peer_effects` (drops dead models) →
`_resync_registry()` — the registry resync **re-adds the just-dropped model
with the same dead key**. Observed in the repro log: the model cycles
cool→drop→re-add→401 again.

---

## Step 1 — exception redesign (`src/llmbroker/exceptions.py`)

Replace the current three-class layout with:

```python
class LLMRequestError(Exception): ...

class NoLLMAvailableError(LLMRequestError):
    def __init__(
        self,
        message: str,
        *,
        reason: str,
        retry_at: datetime | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.retry_at = retry_at
```

`reason` values (plain strings, document them in the class docstring, 1-2
lines max):

| reason | meaning |
|---|---|
| `"empty_pool"` | pool has zero slots (registry empty is caught earlier; this is "everything was dropped") |
| `"no_keys"` | slots exist but none has a resolved key — message must keep the actionable hint "set at least one env var or configure a secrets backend" |
| `"all_disabled"` | keyed slots exist but every one is admin-disabled |
| `"excluded"` | every candidate was excluded for this request (client 4xx / dead key) — internal, the router converts it (Step 3) |
| `"timeout"` | deadline expired (or `wait=0` and nothing free right now); `retry_at` carries the earliest `cooldown_until` among candidates when known |

Delete `AllLLMsFailedError` entirely. Update `src/llmbroker/__init__.py`
imports and `__all__`. Grep for remaining references:

```bash
grep -rn "AllLLMsFailedError" src/ tests/ docs/ README.md specs/
```

Every test that expects `AllLLMsFailedError` (e.g.
`tests/test_router.py::test_missing_api_key_raises_all_llms_failed`,
`test_zero_keyed_configs_raises_immediately_with_default_wait`) now expects
`NoLLMAvailableError` with `reason == "no_keys"`.

## Step 2 — `LLMPool.acquire`: deadline, exclusion, exhaustion detection (`src/llmbroker/broker/pool.py`)

New signature:

```python
async def acquire(
    self,
    deadline: float | None,          # time.monotonic() deadline; None = wait forever
    *,
    operation: str | None = None,
    exclude: frozenset[str] = frozenset(),
) -> LLMConfig:
```

The caller (router) computes the deadline once per request. Semantics inside
the loop, in this exact order on every iteration:

1. `candidates = [s for s in self._slots.values() if s.key is not None and not s.disabled and s.config.name not in exclude]`
2. `avail = [s for s in candidates if (cap is None or in_flight < cap) and (cooldown_until is None or cooldown_until <= now)]`
   — same criteria as the current `_available`, minus the key/disabled checks
   already applied in (1).
3. If `avail`: pick `min(avail, key=lambda s: (self._is_demoted(...), s.order))`,
   bump `in_flight`, return — unchanged selection rule.
4. If `candidates` is empty — **nothing can ever free itself; raise
   immediately regardless of deadline** (`NoLLMAvailableError`):
   - some slot was excluded (i.e. `exclude` intersects slot names) → `reason="excluded"`;
   - `len(self._slots) == 0` → `reason="empty_pool"`;
   - no slot has a key → `reason="no_keys"` (with the env-var hint in the message);
   - otherwise (keyed slots exist, all disabled) → `reason="all_disabled"`.
5. If `deadline is not None and time.monotonic() >= deadline`: raise
   `NoLLMAvailableError(reason="timeout", retry_at=<earliest cooldown_until
   among cooling candidates, or None>)`.
6. Otherwise wait on the condition with
   `timeout = min(<seconds to nearest candidate cooldown expiry>, <seconds to deadline>)`
   (each part only if present; both absent → wait without timeout). This
   replaces `_wake_timeout`; rewrite it to take `candidates` so keyless /
   disabled / excluded slots never contribute a wake-up.

Notes:
- `wait=0` maps to `deadline = time.monotonic()` in the router — step 5 fires
  on the first pass when nothing is free, so `wait=0` is exactly
  "non-blocking" and no longer needs a special branch inside `acquire`; delete
  the current `if wait == 0` branch.
- Blocking is now allowed **only** when `candidates` is non-empty — a
  parallel-capped slot frees on `release`, a cooling slot frees on its timer.
  This is the Bug A fix at the pool level.
- `LLMPool` may import from `llmbroker.exceptions` (no cycle: exceptions.py
  imports nothing from the package).

Update every `pool.acquire(...)` call in `tests/test_pool.py` mechanically:
`acquire(None)` stays, `acquire(0)` → `acquire(time.monotonic())`,
`acquire(N)` → `acquire(time.monotonic() + N)`.

## Step 3 — router: request deadline, 4xx failover without cooldown (`src/llmbroker/broker/router.py`)

### 3a. Request-level deadline

In `Router.chat`:

```python
deadline = None if wait is None else time.monotonic() + wait
client_failed: set[str] = set()
last_client_error: httpx.HTTPStatusError | None = None
while True:
    try:
        config = await self._pool.acquire(
            deadline, operation=operation, exclude=frozenset(client_failed)
        )
    except NoLLMAvailableError as exc:
        if exc.reason == "excluded" and last_client_error is not None:
            raise last_client_error from None
        raise
    ...
```

- Delete the current pre-loop zero-keyed check (`router.py:68-77`) — Step 2's
  `reason="no_keys"` subsumes it, including the wait-independent guarantee
  (candidates empty → immediate raise even with `wait=None`).
- Delete the `except TimeoutError` translation — `acquire` no longer raises
  `TimeoutError`.
- Delete both `if wait == 0: raise NoLLMAvailableError(...)` branches inside
  `_attempt` (`router.py:176-177`, `183-184`): after any failure the loop just
  re-enters `acquire`, and an expired deadline with nothing instantly free
  raises `reason="timeout"` there. With `wait=0` and a second model free,
  failover now happens — this is the intended change.

### 3b. Error classification in `_attempt`

Add module-level helper:

```python
def _is_client_error(code: int) -> bool:
    return 400 <= code < 500 and code not in (429, 401, 403)
```

`_attempt` return type becomes `AsyncResult | httpx.HTTPStatusError | None`:

- **success** → `AsyncResult` (unchanged).
- **429/503** → cooldown from `Retry-After`/default with backoff, record with
  `cooldown_delay`, return `None` (unchanged).
- **401/403** → keep the current path (cooldown + record with
  `cooldown_delay`; the learning hook drops the model), **and** the router
  loop additionally adds the name to `client_failed` — a dead key cannot heal
  mid-request, so the same request must not re-try that model even when
  `optimize=False` (no learning hook, no drop).
- **other 4xx (client error)** → **no `cool_down`**. Call
  `await self._pool.release(config)` instead (the slot was acquired;
  `cool_down` used to do the `in_flight` decrement — without it `release` is
  mandatory, do not forget this). Record the journal row as
  `CallStatus.ERROR` with `http_status`, `error_detail`, **no**
  `cooldown_delay` (so no `cooldown_until`/`key_hash` on the row — it must
  not participate in the shared-cooldown rebuild). Return the caught
  `httpx.HTTPStatusError` instance.
- **5xx (non-503) and network errors** (`TimeoutException`, `ConnectError`,
  `OSError`) → cooldown + record, return `None` (unchanged).

The `Router.chat` loop handles the three outcomes:

```python
outcome = await self._attempt(...)
if isinstance(outcome, AsyncResult):
    return outcome
if isinstance(outcome, httpx.HTTPStatusError):
    client_failed.add(config.name)
    last_client_error = outcome
# None or client error → loop
```

For the 401/403 case `_attempt` returns `None` but the router must still add
the name to `client_failed`; simplest: also return the `HTTPStatusError` for
401/403 **after** the cooldown/record calls, and let the router treat any
returned exception as "exclude + remember". Then "raise last_client_error"
must only fire for genuine client errors: track them separately —
`last_client_error` is set **only** when `_is_client_error(code)` is true;
for 401/403 add to `client_failed` but leave `last_client_error` alone (if
the whole pool died of 401s, `acquire` raises `reason="excluded"` and, with
`last_client_error is None`, the router re-raises that `NoLLMAvailableError`
as-is — acceptable: the log already carries the dead-key error lines).
Concretely: return a small tuple or set a flag — recommended shape:

```python
# _attempt returns AsyncResult | _Failed | None
@dataclass(frozen=True)
class _Failed:
    exclude: bool
    error: httpx.HTTPStatusError | None   # set only for _is_client_error codes
```

(`None` keeps meaning "cooled down, try next".) Pick this shape; it keeps the
loop explicit and typed.

### 3c. Under-provisioned alert

`AsyncBroker._maybe_alert_underprov` (`src/llmbroker/broker/broker.py:261`)
now fires only when it makes sense: guard with
`if exc.reason != "timeout": return` (pass the exception in, or check reason
at the call sites in `ask`/`chat`). Reasons `no_keys`/`empty_pool`/
`all_disabled` have their own error lines already; the "all COOLING" warning
is only meaningful on a timeout.

## Step 4 — fix dead-key resurrection (Bug B, `src/llmbroker/broker/learning.py`)

In `maybe_rebuild`, reorder so the registry resync happens **before** the
journal-derived effects:

```python
self._next_rebuild = now + _REBUILD_TTL
if resync_registry:
    await self._resync_registry()
if isinstance(self._inner, QueryableStoreProtocol):
    rows = await self._inner.calls(limit=self._quality_rebuild_limit)
    self._apply_scores_and_metrics(rows)
    await self._apply_peer_effects(rows)
await self._resync_disabled()
```

Effect: the resync may re-add a dropped model, but `_apply_peer_effects` in
the same pass re-drops it while its 401/403 row (with matching `key_hash`) is
still inside the tail. When the admin replaces the secret, the re-resolved key
gets a different hash, the old 401 rows stop matching, and the model revives
on the next rebuild — this is the intended recovery path; state it in
`optimizer.md` (Step 8).

Ordering detail that makes this sound: `Router._attempt` awaits
`record()` (journal write) before returning, and `_drive` forces a rebuild on
the failure — so the 401 row is always already in the journal when the rebuild
reads the tail.

## Step 5 — remove `mark_quality_fail` (dead/spec-violating code)

Quality must reorder selection, never touch availability bookkeeping
(`optimizer.md`: "it reorders selection, it never excludes"). The
`score == 0.0` → `fail_count += 1` path is untested and conflates the axes.

- Delete `LLMPool.mark_quality_fail` (`src/llmbroker/broker/pool.py:204-207`).
- In `AsyncResult.record_quality` (`src/llmbroker/broker/result.py:41-49`)
  delete the `if score == 0.0: ...` branch.
- Drop the now-unused `pool` constructor parameter from `AsyncResult` and its
  construction site in `Router._attempt`; remove the `# noqa: PLR0913` if the
  parameter count falls under the limit.
- Grep `mark_quality_fail` across `src/` and `tests/` — remove leftovers.

## Step 6 — one shared HTTP client per broker (`src/llmbroker/chat.py`, router, broker)

Currently `call_provider` opens a fresh `httpx.AsyncClient` per call — no
connection/TLS reuse.

- In `chat.py` add:

  ```python
  def make_client() -> httpx.AsyncClient:
      return httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
  ```

  It must construct the client via this module's `httpx` reference — the
  existing tests patch `llmbroker.chat.httpx.AsyncClient` and must keep
  working without edits.
- `call_provider` gains `client: httpx.AsyncClient | None = None`. With a
  client passed, use it directly (no `async with` — do not close the shared
  client); with `None`, keep the current ephemeral behavior (back-compat for
  direct users of `call_provider`, it is in `chat.__all__`).
- `Router` lazily creates the client on first `_attempt`
  (`self._http: httpx.AsyncClient | None = None; if self._http is None:
  self._http = make_client()`), passes it to `call_provider`, and exposes
  `async def aclose(self)` that closes it (idempotent, `None`-safe).
- `AsyncBroker.aclose` calls `await self._router.aclose()` in addition to the
  existing port loop.
- Lazy creation matters: existing tests wrap the ask/chat call in
  `with patch("llmbroker.chat.httpx.AsyncClient", ...)` — the client is then
  created inside the patch. Tests that construct a broker and call it under
  several sequential patches now reuse the first patched instance within one
  broker; check `tests/test_broker_integration.py` and
  `tests/test_optimizer_integration.py` for tests that patch different
  responses across calls on the same broker and, where needed, patch
  `llmbroker.broker.router.call_provider` instead (simpler and transport-
  independent).

## Step 7 — small simplifications

### 7a. CLI `env` reuses the Registry capability (`src/llmbroker/cli.py:34-59`)

`_cmd_env` re-parses the TOML by hand. Replace with the existing capability:

```python
reg = Registry(toml_path)
configs = asyncio.run(reg.load())        # llms in file order
infos = asyncio.run(reg.key_info())      # ref -> KeyInfo
refs = deduped api_key_ref list in configs order
```

Delete `_api_key_refs` and the manual `tomllib` load in `_cmd_env` (keep the
"no such file" check). Output format must stay byte-identical —
`tests/test_cli_env.py` asserts it; do not change those tests, they are the
contract.

### 7b. Sync facade: drop the `*_of` quartet (`src/llmbroker/sync.py`)

`LLM` holds the `AsyncLLM` handle directly:

```python
class LLM:
    def __init__(self, run_fn, async_llm): ...
    @property
    def config(self): return self._async.config          # plain attribute, no loop hop
    @property
    def disabled(self): return self._async.disabled      # dict read, thread-safe enough
    def state(self): return self._run(self._async.state())
    def metrics(self): return self._run(self._async.metrics())
```

`Broker.get(name)` becomes
`return LLM(self._run, self._run(self._async.get(name)))`.
Delete `config_of`, `disabled_of`, `state_of`, `metrics_of` from `Broker`.

### 7c. Metrics-map dedup

`AsyncLLM.metrics` (`result.py:78-85`) and `PoolView._metrics_map`
(`pool_view.py:31-37`) duplicate the same three-branch logic. Add one helper
in `learning.py` next to `metrics_from_calls`:

```python
async def resolve_metrics_map(store: StoreProtocol) -> dict[str, LLMMetrics]:
    # _LearningHook -> its metrics_cache; QueryableStoreProtocol -> read tail
    # and derive; otherwise -> {}
```

Both call sites delegate to it; `PoolView._metrics_map` disappears.

### 7d. Tool-loop dedup (`src/llmbroker/chat.py:138-185`)

Extract the shared per-step logic:

```python
def _advance_tool_loop(convo: list[dict], result, dispatch) -> str | None:
    """Append the assistant turn and tool results; return final text when done."""
    if not result.tool_calls:
        return result.text
    convo.append({"role": "assistant", "content": result.text or None,
                  "tool_calls": result.tool_calls})
    convo.extend(execute_tool_calls(result.tool_calls, dispatch))
    return None
```

`arun_tool_loop` / `run_tool_loop` keep only their own `await llms.chat` /
`llms.chat` line plus the loop shell. Behavior identical, including the
`return ""` after `max_steps`.

## Step 8 — spec updates (`specs/reference/`)

Specs state current behavior only — no "previously X" narration, no
implementation details (no signatures/field names).

- `architecture.md`:
  - the routing paragraph gains the current error contract: one exception for
    "no model available" carrying a machine-readable reason and, when the pool
    is only temporarily exhausted, the earliest time a model returns; client
    request errors (4xx other than quota/auth) fail over without cooling
    anything and surface the provider error when every model rejects the
    request.
  - `wait` documented as the deadline of the whole call; `wait=0` =
    non-blocking with failover across currently-free models; `wait=None`
    waits while at least one model will return by itself, and errors
    immediately when none can.
- `optimizer.md`:
  - the dead-key paragraph: a dead key drops the model and the drop holds as
    long as journal rows with the same key digest remain in the rebuild tail;
    replacing the secret revives the model on a following rebuild.
  - cooldown section: only quota/availability failures (429/503, auth, 5xx,
    network) cool a model; client request errors never do.
- Check `docs/src/en/*.md`, `docs/src/ru/*.md`, `README.md` for mentions of
  `AllLLMsFailedError` or per-attempt `wait` semantics and update the wording
  (do not execute doc snippets; text edits only).

## Step 9 — tests

Run everything with the done gate after each step batch: `invoke pre` and
`python -m pytest` both green; testcontainers work locally on macOS — never
skip Postgres/Mongo tests.

### 9a. Regression tests for the confirmed bugs (write first, keep forever)

`tests/test_dead_key_exhaustion.py`:

1. `test_single_dead_key_raises_instead_of_hanging` — one model, TOML
   registry, env key set; patch `llmbroker.broker.router.call_provider` to
   raise `httpx.HTTPStatusError` with status 401; `await
   asyncio.wait_for(broker.ask("hi"), timeout=5)` must raise
   `NoLLMAvailableError` (any terminal reason), **not** `TimeoutError`.
2. `test_dead_key_drop_survives_rebuild` — after the 401, force
   `broker._learning_hook.maybe_rebuild(force=True)` twice; the model must not
   reappear in `snapshot()` / must not be re-attempted (assert
   `call_provider` invocation count did not grow).
3. `test_replacing_secret_revives_model` — same setup with a
   `DictSecrets`-backed mutable secrets store: change the secret value, force
   a rebuild, model is back (`snapshot()` shows it with `has_key=True`).

### 9b. Router behavior updates (`tests/test_router.py`)

- Rewrite `wait=0` tests: two models, first returns 429 → second answers
  (failover now happens at `wait=0`); single model 429 + `wait=0` →
  `NoLLMAvailableError` with `reason="timeout"`.
- `test_wait_bounds_whole_request` — models that fail fast repeatedly;
  `wait=0.5` → the call raises within ~1s wall clock (generous bound, no
  flakiness).
- Client-error suite: 400 → no `cooldown_until` on the journal row, model not
  cooling, next model tried; both models 400 → the second
  `httpx.HTTPStatusError` propagates to the caller; a later request on the
  same broker may use both models again (exclusion is per-request).
- `no_keys` / `empty_pool` / `all_disabled` reason assertions.
- `reason="timeout"` carries `retry_at` when models are cooling.

### 9c. Cluster shared-cooldown e2e (mission №6) — `tests/test_cluster_cooldown.py`

Two `AsyncBroker` instances over one sqlite file (source-dispatch string form
`str(tmp_path / "b.db")`), same env key:

1. Broker A gets a 429 with `Retry-After: 120` (patched provider) → row
   journaled with `cooldown_until` + `key_hash`.
2. On broker B, force `maybe_rebuild(force=True)`; `snapshot()` on B shows the
   model with a `cooldown_until` in the future.
3. Negative: give B a different key value for the same ref (own `DictSecrets`)
   → after rebuild B's model is **not** cooling (429 follows the key).
4. 5xx variant: broker A gets 503 → cooldown applies on B **regardless** of
   key difference.

### 9d. Per-user scoping e2e (mission №4) — `tests/test_scope_dead_key.py`

Shared sqlite backend; secrets contain `KEY` (good) and `u1/KEY` (value that
the fake provider rejects with 401). `AsyncBroker(..., scope="u1")` and an
unscoped broker:

- u1's broker: model dropped after the 401; unscoped broker still answers.
- journal rows from u1's calls carry `scope="u1"`;
  `u1_broker.calls(limit=10)` returns only u1 rows, the unscoped broker's
  `calls()` only unscoped rows (current scoped filtering is the contract).

### 9e. Real-transport tests — `tests/test_transport.py`

Fixture: build an `AsyncBroker`, then
`patch("llmbroker.chat.make_client", return_value=httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=1.0))`
so the full request/response path (headers, JSON) is exercised:

- `Retry-After` as HTTP-date → cooldown ≈ parsed seconds (assert the journal
  row's `cooldown_until` within a tolerance window).
- `tool_calls` response body round-trips through `broker.chat(tools=...)`.
- `usage` with provider extras lands in `Usage.extra`.
- Connection reuse: one broker, two sequential asks — `make_client` called
  exactly once.

### 9f. CLI round trip (mission №2) — extend `tests/test_cli.py`

Write a minimal preset TOML with a `[keys.REF]` help section; then:

1. `main(["env", preset])` output contains the ref and its help line.
2. `main(["sync", preset, str(db)])` returns 0.
3. `AsyncBroker(str(db))` with env key + patched provider answers `ask()`.

### 9g. Cheap-at-low-usage (mission №8) — `tests/test_store_traffic.py`

`CountingStore` test double wrapping `InMemoryStore`, counting `record` and
`calls` invocations:

- `optimize=False`: N successful asks → `calls` (journal reads) invoked 0
  times; `record` invoked N times.
- `optimize=True`: N successful asks in quick succession → `calls` invoked
  exactly once (the provision-time warm start); success paths never force a
  rebuild.

### 9h. `parallel` cap (mission №8) — extend `tests/test_pool.py` or new file

Fake provider that bumps an in-flight counter and blocks on an
`asyncio.Event`; fire 3 concurrent `ask()`s:

- `parallel=1`: observed max concurrency is 1.
- `parallel=None` (control): observed max concurrency is 3.

Deterministic via events — no sleeps, no timing assertions.

### 9i. Mechanical updates

- `tests/test_pool.py` — `acquire` signature (Step 2).
- Any test asserting the old `wait=0`-raises-after-first-failure behavior or
  `AllLLMsFailedError`.
- `tests/test_broker_integration.py::test_all_offline_raises_and_alerts` —
  align with the new rule: the under-provisioned warning fires on
  `reason="timeout"`; use a finite `wait` in the test.

## Execution order

1. Step 1 (exceptions) + Step 2 (pool) + Step 3 (router) + their test updates
   — one coherent batch, `invoke pre` + full pytest.
2. Step 4 (rebuild order) + 9a regression tests.
3. Step 5 (mark_quality_fail) — tiny, own batch.
4. Step 6 (shared client) + 9e.
5. Step 7a-7d simplifications — behavior-neutral, existing tests must stay
   green untouched (except 7b's internals if any test pokes `*_of`).
6. Steps 9b-9h remaining new tests.
7. Step 8 spec/doc updates last, describing the now-current behavior.

Done gate for every batch: `invoke pre` clean, `python -m pytest` all passed,
zero skipped-as-green surprises. Do not bump the version.

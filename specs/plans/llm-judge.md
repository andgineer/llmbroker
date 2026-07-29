# LLM-as-judge quality scoring

**Source of truth: https://github.com/andgineer/llmbroker/issues/8** — the deliverable is the
functionality described there. This plan is the suggested route; if the code has drifted from
what the plan assumes, the issue wins.

## Context

Today the quality loop is host-driven only:

- `AsyncResult.record_quality(score)` (`src/llmbroker/broker/result.py`) and
  `AsyncBroker.record_quality(...)` (`src/llmbroker/broker/broker.py`) append a self-contained
  quality record and — when the optimizer is on — fold the score into the in-memory
  per-`(model, operation)` window via `_LearningHook.record_quality`
  (`src/llmbroker/broker/learning.py`).
- `Optimizer` (`src/llmbroker/optimizer.py`) is a dataclass whose fields are the tuning knobs
  (`quality_floor`, `quality_window`, ...); `is_demoted` drives the pool's demoted-last
  selection order.
- `Router.chat` (`src/llmbroker/broker/router.py`) routes one completion over the pool with
  failover, journals every attempt, and returns an `AsyncResult` carrying `llm_name`,
  `operation`, `call_id`. `AsyncBroker.ask`/`chat` are thin delegations to it.
- A host that never calls `record_quality` gets no quality signal — demotion stays inert.

The judge closes this loop automatically: sample a fraction of successful replies, score each
sampled reply with an LLM through the broker's own routing, and feed the score into the
existing `record_quality` path verbatim.

## Design decisions

The issue fixes the shape; these are the interpretation points this plan commits to. Do not
re-decide them during implementation:

1. **The knob is `Optimizer(judge_fraction=...)`, default `0.0`.** At `0.0` (or with
   `optimize=False`) the broker constructs no judge object at all — no tasks, no sampling
   bookkeeping, no behavior change. `judge_fraction >= 1.0` judges every reply.
2. **"Low-priority" means `wait=0` plus silent skip.** The judge call uses the non-blocking
   deadline: it takes a currently-free slot or nothing, never waits on a cooldown or a busy
   slot, and a `NoLLMAvailableError` (or a provider error surfaced by the router) skips the
   sample silently (`logger.debug` at most). Host traffic never queues behind judge traffic.
3. **Dedicated operation constant** — `"llmbroker.judge"` — the dotted namespace avoids
   collision with host operation names. Judge calls are journaled by the router like any other
   call under this operation (dogfooding: failover, cooldowns, metrics, `calls()` visibility
   all apply). A host that wants its own numbers without this traffic filters by operation —
   `journal-stats-window.md` carries that filter on `calls()` and `stats()` for this reason.
4. **Sampling is a deterministic per-bucket accumulator**, not RNG: per `(model, operation)`
   key, `acc += judge_fraction; if acc >= 1.0: acc -= 1.0; sample`. Exact fraction per bucket,
   trivially testable, no seeding. The accumulator is in-memory per broker instance —
   session-scoped, like the optimizer's backoff counters.
5. **The judge runs as a fire-and-forget `asyncio.Task`** — it must never add latency to the
   host's call. Tasks are tracked in a set; `aclose()` cancels any still pending (before the
   router's HTTP client closes). A `drain()` coroutine awaits all pending tasks — used by
   tests for determinism.
6. **No recursion, structurally.** The sampling hook lives in `AsyncBroker.ask`/`chat`; the
   judge issues its call through `Router.chat` directly, so judge traffic never passes the
   hook. No operation-name check needed.
7. **The judge rubric asks for an integer 0-10, mapped to `score / 10` clamped to `[0, 1]`.**
   Small free-tier models follow an integer scale more reliably than a float. A judge reply
   already in `0-1` form (e.g. `"0.7"`) is NOT special-cased — it parses to `0.07`; accepted
   noise, do not add heuristics. The judge prompt contains the sampled call's last user
   message and the reply text, each truncated to a constant cap — free-tier quota is precious,
   and the judge needs the request context, not the whole transcript.
8. **The judge's own reply gets a mechanical rating on the judge bucket**: parse success
   records `1.0`, parse failure records `0.0`, on `(judge_llm_name, "llmbroker.judge")`. This
   is what makes the issue's "its own demotion verdicts" real — a model that cannot follow the
   rubric demotes for the judge operation and stops being picked as a judge (demoted-last),
   without touching any host bucket. No LLM call is involved, so this is not recursion.
9. **Only plain text replies are sampled**: skip replies with `tool_calls` or empty `text`,
   and requests whose last user message has non-string content — there is nothing for the
   rubric to score. Skips happen before the accumulator advances.
10. **No new storage or state subsystem.** Judge scores go through the existing
    `record_quality` path on the broker's effective store (the `_LearningHook` when the
    optimizer is on): journal append plus immediate in-memory fold, shared with peers via the
    normal rebuild. Each sampled reply adds up to three journal rows (one judge call, one
    host-bucket quality, one judge-bucket quality) to the shared rebuild tail
    (`quality_rebuild_limit`) — the same accepted crowding trade-off already documented in
    `specs/reference/optimizer.md`.

## Design constraints (from the issue's acceptance criteria)

- `judge_fraction=0.0` (default): no LLM calls, no behavior change.
- Sampled replies are scored and fed into `record_quality()` on the sampled call's own
  `(model, operation)` bucket — with the sampled call's `call_id` as the opaque passthrough
  and the broker's `scope` as attribution.
- Judge traffic runs through the broker's own routing under the dedicated operation.
- Judge calls are never judged.
- Graceful no-op when no LLM is available for the judge call.

## Executor guardrails

Repo conventions (CLAUDE.md) this task touches — violations fail the done gate:

- All imports at module top level; no local imports, no `from __future__ import annotations`.
- `broker/__init__.py` stays empty — `_Judge` lives in its own named module and every caller
  imports from that module.
- Comments and docstrings in English, 1-3 lines, no architecture essays; never mention this
  plan file or its step numbers in code.
- `pytest.ini` runs `--doctest-modules`: any doctest you write in `src/` executes as a test.
- Never call ruff directly — run `invoke pre`. Never skip tests to hide failures.
- If ruff flags `PLR0913` (too many args) on a constructor, follow the existing
  `# noqa: PLR0913` pattern (see `AsyncResult.__init__`).

## Steps

### 1. The knob

`src/llmbroker/optimizer.py`: add `judge_fraction: float = 0.0` to the `Optimizer` dataclass,
next to the other quality knobs, with a short trailing comment (e.g.
`# fraction of replies scored by an LLM judge; 0.0 = off`). No validation — `<= 0.0` is off,
`>= 1.0` judges every reply.

### 2. The judge collaborator

New module `src/llmbroker/broker/judge.py`. Contents, top to bottom:

**Constants:**

```python
JUDGE_OPERATION = "llmbroker.judge"
_INPUT_CAP = 4000  # chars kept from each of request and reply in the judge prompt

_JUDGE_PROMPT = """You are a strict quality judge. Rate how well the reply answers the request.
Respond with ONLY one integer from 0 (useless) to 10 (perfect).

Request:
{request}

Reply:
{reply}"""
```

**Score parser** — pure function with a doctest (it will run under `--doctest-modules`;
mirror the `wilson_upper` style):

```python
def parse_judge_score(text: str) -> float | None:
    """First number in ``text`` mapped from the 0-10 scale into [0, 1]; None when absent.

    >>> parse_judge_score("7")
    0.7
    >>> parse_judge_score("Score: 8.5/10")
    0.85
    >>> parse_judge_score("15")
    1.0
    >>> parse_judge_score("no idea") is None
    True
    """
```

Implementation: `re.search(r"\d+(?:\.\d+)?", text)`; no match → `None`; otherwise
`min(float(match) / 10, 1.0)`. No lower clamp needed — the regex matches no minus sign.

**The `_Judge` class** (leading underscore mirrors `_LearningHook`):

```python
class _Judge:
    def __init__(
        self,
        router: Router,
        store: StoreProtocol,
        *,
        judge_fraction: float,
        scope: str | None,
    ) -> None:
```

State: `self._acc: dict[tuple[str, str | None], float]` (sampling accumulators) and
`self._tasks: set[asyncio.Task]`.

```python
def after_reply(self, messages: list[dict], result: AsyncResult, *, trace_id: str | None) -> None:
```

A **synchronous** method (it schedules, never awaits):

1. If `result.tool_calls is not None or not result.text` → return.
2. `request` = content of the last message with `role == "user"` and `str` content
   (iterate `reversed(messages)`); none found → return.
3. Advance the accumulator for `(result.llm_name, result.operation)` by `judge_fraction`;
   if it is still `< 1.0` → return; otherwise subtract `1.0` and continue.
4. `task = asyncio.create_task(self._judge_one(request, result, trace_id))`;
   `self._tasks.add(task)`; `task.add_done_callback(self._tasks.discard)`.

```python
async def _judge_one(self, request: str, result: AsyncResult, trace_id: str | None) -> None:
```

1. Build the prompt: `_JUDGE_PROMPT.format(request=request[:_INPUT_CAP], reply=result.text[:_INPUT_CAP])`.
2. `judge_result = await self._router.chat([{"role": "user", "content": prompt}],
   operation=JUDGE_OPERATION, trace_id=trace_id, wait=0)` — the sampled call's `trace_id` is
   inherited so the judge call lands in the host's trace.
3. Error policy around step 2: `except (NoLLMAvailableError, httpx.HTTPStatusError)` →
   `logger.debug(...)` and return (the router already journaled whatever happened);
   `except Exception` → `logger.exception(...)` and return, never propagate (add
   `# noqa: BLE001` like `Router._log_call`). Do not catch `asyncio.CancelledError`
   explicitly — it does not inherit from `Exception` on Python 3.11+.
4. `score = parse_judge_score(judge_result.text)`.
5. If `score is not None`:
   `await self._store.record_quality(result.llm_name, result.operation, score,
   call_id=result.call_id, scope=self._scope)` — the host bucket, first.
6. Always (after step 5):
   `await self._store.record_quality(judge_result.llm_name, JUDGE_OPERATION,
   1.0 if score is not None else 0.0, scope=self._scope)` — the mechanical judge-bucket
   rating.

```python
async def drain(self) -> None:   # await all pending judge tasks — test determinism
async def aclose(self) -> None:  # cancel pending tasks, then gather(..., return_exceptions=True)
```

Imports needed: `asyncio`, `logging`, `re`, `httpx`, `Router`, `AsyncResult`,
`NoLLMAvailableError`, `StoreProtocol`. No cycle: `router.py` does not import `judge.py`.
Logger name: `logging.getLogger("llmbroker.broker")` like the sibling modules.

### 3. Wiring in `AsyncBroker`

`src/llmbroker/broker/broker.py`:

In `__init__`, right after `self._router = Router(...)`:

```python
self._judge: _Judge | None = None
if self._optimizer is not None and self._optimizer.judge_fraction > 0.0:
    self._judge = _Judge(
        self._router,
        effective_store,
        judge_fraction=self._optimizer.judge_fraction,
        scope=scope,
    )
```

In `ask()` — restructure so the hook runs on the success path only:

```python
try:
    result = await self._router.ask(prompt, operation=operation, trace_id=trace_id, wait=wait)
except NoLLMAvailableError as exc:
    self._maybe_alert_underprov(exc)
    raise
if self._judge is not None:
    self._judge.after_reply([{"role": "user", "content": prompt}], result, trace_id=trace_id)
return result
```

In `chat()` — identical shape, passing the caller's `messages` to `after_reply`.

In `aclose()` — first line, before `await self._router.aclose()` (no judge task may run
against a closed HTTP client):

```python
if self._judge is not None:
    await self._judge.aclose()
```

No sync-wrapper changes: the knob rides on `Optimizer`, which `Broker(optimize=...)`
(`src/llmbroker/sync.py`) already passes through, and judge tasks run on the wrapper's
background event loop.

### 4. Tests

New `tests/test_judge.py`, reusing the `_registry` / `_http_ok` / `_http_error` helpers from
`tests/test_broker.py` (copy them — tests do not import from other test modules) and the same
`patch("llmbroker.chat.httpx.AsyncClient", return_value=...)` mock pattern.

Practical notes that make these tests simple:

- One mock client answers **both** the host call and the judge call with the same text, so
  `_http_ok("7")` alone covers most tests: the host reply is `"7"` and the judge scores it as
  `0.7`. No sequenced mock needed.
- Journal assertions need a queryable store: use `FileStore(tmp_path)` or a sqlite store —
  `InMemoryStore` has no `calls()`.
- After the `ask()` under test, run `await broker._judge.drain()` before asserting. Tests may
  touch private attributes (`_judge`, `_optimizer`) — tests are exempt from strict lint.
- Set secrets via `DictSecrets({"K": "x"})` as the existing broker tests do.

Cover:

- **Default off**: `optimize=True`, `judge_fraction` unset — `broker._judge is None`; one
  `ask()` produces exactly one journal row (the host call), no quality rows.
- **Sampled and scored**: `Optimizer(judge_fraction=1.0)`, mock `_http_ok("7")` — after
  drain, the journal holds: the host call row; a judge call row with
  `operation == "llmbroker.judge"`; a `kind == "quality"` row with `quality_score == 0.7` on
  the host `(llm_name, operation)` carrying the host call's `call_id`; and a judge-bucket
  quality row with `1.0`. The optimizer window folded the `0.7`
  (`broker._optimizer.wilson_bound(...) is not None`).
- **Deterministic fraction**: `judge_fraction=0.5`, four `ask()` calls — replies 2 and 4 are
  judged (two judge call rows).
- **No recursion**: `judge_fraction=1.0`, one `ask()` — after drain, exactly two
  `kind == "call"` rows total (host + judge), and exactly one judge call row.
- **Graceful skip**: make the judge's router call raise `NoLLMAvailableError` (e.g. patch
  `broker._judge._router.chat` with an `AsyncMock(side_effect=NoLLMAvailableError(...))`
  after the host call) — drain raises nothing, no quality rows appended.
- **Parse failure**: mock reply `"looks good to me"` — no host-bucket quality row; the judge
  bucket gets `0.0`.
- **Judge demotion closes its own loop**: `judge_fraction=1.0`, unparseable judge replies,
  >= `quality_min_count` (10) `ask()` calls — `broker._optimizer.is_demoted(name,
  JUDGE_OPERATION)` becomes `True` (mirror `tests/test_optimizer_integration.py`; ten `0.0`
  scores put the Wilson upper bound below the `0.3` floor).
- **Not sampled**: a reply with `tool_calls`, and an empty-text reply — no judge task
  (`broker._judge._tasks` empty, accumulator unchanged).
- **`aclose()` with a pending judge task** neither hangs nor raises (block the mock's judge
  response on an `asyncio.Event`, call `aclose()`).
- **Sync smoke** (extend `tests/test_sync.py`): `Broker(..., optimize=Optimizer(judge_fraction=1.0))`,
  one `ask()`, then drain through the wrapper's internals
  (`broker._run(broker._async._judge.drain())`), assert the host-bucket quality row exists.
- Parser edge cases are the doctest in step 2 — no separate test file needed.

### 5. Docs and specs

- `specs/reference/optimizer.md`: a new "LLM-as-judge" section — behavior-level, current-state
  prose (sampling fraction knob, dedicated low-priority operation, scores feed the existing
  quality loop, judge calls never judged, graceful skip). No signatures or field names beyond
  the public knob (spec rules in CLAUDE.md).
- `specs/reference/architecture.md`: drop the "Not yet implemented" table (its only row is
  this feature) and mention the judge in the Core section's optimizer bullet.
- `docs/src/en/usage.md` and the mirrored `docs/src/ru/usage.md`: after the quality-rating
  section, a short "automatic scoring" paragraph with an
  `Optimizer(judge_fraction=0.1)` example. Keep both language versions in sync; the ru page
  is in Russian.
- `README.md`: optional single-line mention in the self-regulating-pool row; skip if it
  doesn't fit naturally.
- Delete this plan file in the final commit of the implementation, per repo convention.

### 6. Done gate

Per CLAUDE.md: `invoke pre` clean and `python -m pytest` all green (doctests run via
`--doctest-modules`; no skipped tests). Run `invoke pre` after each discrete batch, not only
at the end.

## Non-goals

- Ambiguous-routing arbitration — explicitly out of scope in the issue.
- Excluding the reply's author from judging its own reply (self-preference bias): accepted for
  v1 — the pool is small, and the router picks the judge by the normal selection order, which
  the judge bucket's own demotions already steer.
- Configurable judge rubric/prompt or per-operation rubrics.
- A rater-identity field on quality records (host ratings and judge ratings land in the same
  bucket by design — "feed the existing loop verbatim").
- Retries for a failed judge attempt: one shot per sample; the router's in-call failover is
  the only retry that happens.
- New store queries, journal lookups, or schema changes — none are needed.

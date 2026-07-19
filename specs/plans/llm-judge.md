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

The issue fixes the shape; these are the interpretation points this plan commits to:

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
   all apply).
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
   Small free-tier models follow an integer scale more reliably than a float. The judge prompt
   contains the sampled call's last user message and the reply text, each truncated to a
   constant cap — free-tier quota is precious, and the judge needs the request context, not
   the whole transcript.
8. **The judge's own reply gets a mechanical rating on the judge bucket**: parse success
   records `1.0`, parse failure records `0.0`, on `(judge_llm_name, "llmbroker.judge")`. This
   is what makes the issue's "its own demotion verdicts" real — a model that cannot follow the
   rubric demotes for the judge operation and stops being picked as a judge (demoted-last),
   without touching any host bucket. No LLM call is involved, so this is not recursion.
9. **Only plain text replies are sampled**: skip replies with `tool_calls` or empty `text` —
   there is nothing for the rubric to score.
10. **No new storage or state subsystem.** Judge scores go through the existing
    `record_quality` path on the broker's effective store (the `_LearningHook` when the
    optimizer is on): journal append plus immediate in-memory fold, shared with peers via the
    normal rebuild. Each sampled reply adds up to three journal rows (one judge call, one host
    -bucket quality, one judge-bucket quality) to the shared rebuild tail
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

## Steps

### 1. The knob

`src/llmbroker/optimizer.py`: add `judge_fraction: float = 0.0` to the `Optimizer` dataclass,
next to the other quality knobs. No validation — `<= 0.0` is off, `>= 1.0` judges every reply.

### 2. The judge collaborator

New module `src/llmbroker/broker/judge.py` (internal — `broker/__init__.py` stays empty per
CLAUDE.md):

- Module constant for the operation name (`"llmbroker.judge"`) and truncation cap.
- A score-parsing helper: extract the first number from the judge's reply text, divide by 10,
  clamp to `[0, 1]`, return `None` when nothing parses. Pure function with a doctest
  (`--doctest-modules` runs it — mirror `wilson_upper`).
- `_Judge` class (naming mirrors `_LearningHook`), constructed with the router, the effective
  store, the optimizer, and the broker scope:
  - `after_reply(messages, result, trace_id)` — the hook: skip non-text replies, advance the
    per-bucket accumulator, and on a sampled reply spawn the judging task into the tracked
    set.
  - The task coroutine: build the judge prompt from the sampled request and reply, call
    `Router.chat` with the judge operation, `wait=0`, and the sampled call's `trace_id` (the
    judge call lands in the host's trace); parse the score; on success
    `store.record_quality(...)` on the sampled call's bucket with its `call_id`; record the
    mechanical `1.0`/`0.0` on the judge bucket. Catch `NoLLMAvailableError` and provider
    errors as a silent skip; anything unexpected logs once via `logger.exception` and never
    propagates.
  - `drain()` — await all pending tasks; `aclose()` — cancel pending tasks and gather them.

### 3. Wiring in `AsyncBroker`

`src/llmbroker/broker/broker.py`:

- In `__init__`, after the router is built: construct `_Judge` iff the optimizer is present
  and `judge_fraction > 0.0`; otherwise the attribute stays `None`.
- In `ask()` and `chat()`, after a successful result: `self._judge.after_reply(...)` (for
  `ask`, the judge context is the single user message built from `prompt`). The hook is
  synchronous scheduling — no `await` on the judge outcome.
- In `aclose()`: close the judge (cancel pending tasks) before `self._router.aclose()`, so no
  judge task runs against a closed HTTP client.

No sync-wrapper changes: the knob rides on `Optimizer`, which `Broker(optimize=...)`
(`src/llmbroker/sync.py`) already passes through, and judge tasks run on the wrapper's
background event loop.

### 4. Tests

New `tests/test_judge.py`, reusing the `_registry` / `_http_ok` mock pattern from
`tests/test_broker.py` (patch `llmbroker.chat.httpx.AsyncClient`); use `drain()` for
determinism. Cover:

- **Default off**: `optimize=True` broker with `judge_fraction` unset — exactly one journal
  call row per `ask()`, no judge object, no quality rows.
- **Sampled and scored**: `judge_fraction=1.0`, judge reply `"7"` — journal shows the judge
  call under the judge operation, and a `kind="quality"` row with `score == 0.7` on the
  sampled call's `(model, operation)` bucket carrying the sampled call's `call_id`; the
  optimizer window folded the score.
- **Deterministic fraction**: `judge_fraction=0.5` — replies 2 and 4 of 4 are judged, per
  bucket.
- **No recursion**: with `judge_fraction=1.0`, one host call produces exactly one judge call.
- **Graceful skip**: the judge's router call raises `NoLLMAvailableError` — no exception
  escapes, no quality rows appended.
- **Parse failure**: judge reply `"looks good to me"` — no host-bucket rating; judge bucket
  gets `0.0`. Parse success gets `1.0` on the judge bucket.
- **Judge demotion closes its own loop**: feed >= `quality_min_count` unparseable judge
  replies from one model; assert the judge operation demotes for it.
- **Tool-call and empty replies are not sampled.**
- **`aclose()` with a pending judge task** neither hangs nor raises.
- **Sync smoke**: `Broker(..., optimize=Optimizer(judge_fraction=1.0))` end-to-end in
  `tests/test_sync.py`.
- Score-parsing helper edge cases via its doctest (integer, float, out-of-range clamp,
  garbage).

### 5. Docs and specs

- `specs/reference/optimizer.md`: a new "LLM-as-judge" section — behavior-level, current-state
  prose (sampling fraction knob, dedicated low-priority operation, scores feed the existing
  quality loop, judge calls never judged, graceful skip). No signatures or field names beyond
  the public knob (spec rules in CLAUDE.md).
- `specs/reference/architecture.md`: drop the "Not yet implemented" table (its only row is
  this feature) and mention the judge in the Core section's optimizer bullet.
- `docs/src/en/usage.md` and the mirrored `docs/src/ru/usage.md`: after the quality-rating
  section, a short "automatic scoring" paragraph with an
  `Optimizer(judge_fraction=0.1)` example. Keep both language versions in sync.
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

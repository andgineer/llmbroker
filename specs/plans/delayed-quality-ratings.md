# Delayed quality ratings

**Source of truth: https://github.com/andgineer/llmbroker/issues/9** — the deliverable is the
functionality described there. This plan is the suggested route; if the code has drifted from
what the plan assumes, the issue wins.

## Context

Quality can currently be recorded only through the result handle:

- `AsyncResult.record_quality(score)` (`src/llmbroker/broker/result.py`) holds `_llm_name`,
  `_operation`, `_call_id` privately and delegates to `StoreProtocol.record_quality(llm_name,
  operation, score, call_id=...)`.
- When the optimizer is on, the broker's `self._store` is the `_LearningHook`
  (`src/llmbroker/broker/learning.py`), whose `record_quality` appends a self-contained quality
  record to the journal **and** folds the score into the in-memory optimizer window. When the
  optimizer is off, `self._store` is the bare store — journal append only.
- Quality journal records are self-contained (`Call` with `kind="quality"`, see
  `src/llmbroker/models.py`): they are never joined against the call row they rate. This is what
  makes arbitrarily late ratings safe — nothing needs to be looked up.

A host that wants to rate a call later (e.g. a user reviews an LLM-produced artifact days after
the call) must persist the rating identity itself. Everything it needs is `(llm_name,
operation)`; `call_id` is an optional opaque passthrough stored on the quality record for host
analytics.

## Design constraints

- **No journal lookup.** Do not implement any `call_id -> (llm_name, operation)` resolution
  against the journal: retention purges old call rows (90 days default,
  `src/llmbroker/backends/ports.py`), so a lookup-based API would break for exactly the
  long-delayed ratings this feature exists for. The host supplies the identity.
- The existing `reply.record_quality(score)` path must keep working unchanged.
- Score semantics stay as they are: host-defined float, 1.0 good / 0.0 bad, not validated.
- Rating an `llm_name` that is unknown or no longer in the pool must not raise — it appends a
  record and folds into a window nobody consults. Harmless by design; cover with a test, do not
  add validation.

## Steps

### 1. Expose rating identity on `AsyncResult`

`src/llmbroker/broker/result.py`: add read-only properties `llm_name`, `operation`, and
`call_id` over the existing private attributes. Keep the private attributes and the existing
`record_quality` as they are.

### 2. Broker-level entry point

`src/llmbroker/broker/broker.py`: add

```python
async def record_quality(
    self,
    llm_name: str,
    operation: str | None,
    score: float,
    *,
    call_id: str | None = None,
) -> None:
```

Behavior: `await self.ensure_pool()` first (matches `ask()`/`chat()`, and guarantees the
warm-start journal rebuild has run before the new score folds into the window), then delegate to
`self._store.record_quality(llm_name, operation, score, call_id=call_id)`. `self._store` is
already the learning hook when the optimizer is on, so no optimizer plumbing is needed here.
Place it near the quality-related or routing methods; 1-3 line docstring (e.g. "Record a quality
score for a past call — the delayed counterpart of `result.record_quality`.").

### 3. Sync mirrors

`src/llmbroker/sync.py`:

- `Result`: add `llm_name`, `operation`, `call_id` properties delegating to `self._async`.
- `Broker`: add `record_quality(llm_name, operation, score, *, call_id=None)` running the async
  method via `self._run(...)`.

### 4. Tests

Every new surface gets tests in the same session (see CLAUDE.md). Suggested placement — follow
the conventions of the named files:

- Result identity: async — wherever `AsyncResult` is already exercised end-to-end; sync — extend
  `tests/test_sync.py`. Assert `llm_name` matches the model that answered, `operation` matches
  what was passed, `call_id` is a non-empty string, and that the same `call_id` lands on the call
  journal row.
- `AsyncBroker.record_quality` appends a self-contained quality record (`kind="quality"`,
  `status is None`, `call_id` passed through) — mirror the pattern of
  `tests/test_store_backends.py::test_record_quality_appends_self_contained_row`, but through the
  broker.
- Delayed rating drives learning: feed >= `quality_min_count` zero scores through
  `AsyncBroker.record_quality` and assert the `(model, operation)` bucket demotes — mirror
  `tests/test_optimizer_integration.py`.
- Optimizer off (`optimize=False`): `record_quality` still appends to the journal and does not
  raise.
- Unknown `llm_name`: does not raise.
- Sync `Broker.record_quality`: end-to-end smoke, extend `tests/test_sync.py`.

### 5. Docs and specs

- `docs/src/en/usage.md` (Quality rating section, ~line 54) and the mirrored
  `docs/src/ru/usage.md`: after the existing `reply.record_quality(0.9)` example, add a short
  "rate it later" paragraph — persist `reply.llm_name` (and the operation you passed), then call
  `llms.record_quality(llm_name, operation, score)` whenever the verdict arrives, e.g. after a
  user review a day later. Keep both language versions in sync.
- `specs/reference/optimizer.md`: one or two sentences of current-state prose stating that
  quality ratings are accepted at any time after the call — not only via the live result — and
  that self-contained quality records are what makes late ratings safe. No signatures or field
  names (spec rules in CLAUDE.md).
- `README.md`: optional single-line mention in the self-regulating-pool row; skip if it doesn't
  fit naturally.

### 6. Done gate

Per CLAUDE.md: `invoke pre` clean and `python -m pytest` all green (doctests run via
`--doctest-modules`; no skipped tests). Run `invoke pre` after each discrete batch, not only at
the end.

## Non-goals

- LLM-as-judge scoring (issue #8).
- Journal lookups by `call_id`, new store queries, or schema changes — none are needed.
- Host-side rating policy (when to rate, partial credit, dedup) — the host's business.

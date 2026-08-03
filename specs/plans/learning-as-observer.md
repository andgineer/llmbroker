# Learning observes the journal; it does not wrap the store

## Goal

Two changes with one subject — what llmbroker learns, and where that learning
lives:

1. `_LearningHook` stops being a store wrapper and becomes an observer.
2. The unmet-budget bound stops being pool-slot state and is derived from the
   journal like everything else.

## Why

**The wrapper breaks `isinstance`.** `learning.py:115` forwards every unknown
attribute to the wrapped store, so `isinstance(hook, QueryableStoreProtocol)` is
true whatever is inside. `broker.py:921-924` says so outright. The broker
therefore carries two names for one thing — `_store` (hook) and `_base_store`
(backend) — with an unwritten rule about which to use where:

| caller | uses | because |
|---|---|---|
| `Router`, `PoolView`, `record_quality` | `_store` | wants the learning |
| `calls()`, `stats()` | `_base_store` | `isinstance` lies on the hook |
| `disable_llm`, `enable_llm`, `sync`'s seed, `_dead` | `_base_store` | capability probe |

Six sites, no compiler checking any of it.

**The budget bound is a second learning subsystem.** `_Slot.unmet_budget` /
`unmet_until`, `note_unmet_budget`, `clear_unmet_budget`, `_over_budget`,
`_UNMET_WINDOW_SEC`, `_UNMET_SLACK_SEC` hold node-local derived state that no
journal read produces. Invariant 8 says everything derived comes from the
journal and there is no second state subsystem. The evidence is already in the
journal — an expired budget is journaled — so the mechanism is redundant with
the debounced rebuild that already derives cooldowns and quality windows.

`decisions.md::budget-expiry-teaches-ordering` stands: the expiry still teaches
ordering, is still budget-relative, and still never withdraws a model. Only
where the derived bound lives changes.

## Part 1 — observer

1. **`learning.py`**: `_LearningHook` becomes `Learner`. It is not a store and
   implements no store protocol. Public surface:
   - `async def observe(call: Call) -> None` — today's `_drive`.
   - `async def maybe_rebuild(*, force=False, resync_registry=True) -> None` —
     unchanged.
   - `def record_quality_observed(llm_name, operation, score) -> None` — the
     optimizer half of today's `record_quality`; the store write moves to the
     caller.
   - `metrics: dict[str, LLMMetrics]` — today's `metrics_cache`.

   Delete `__getattr__`, `record`, `record_quality`, `calls`.

2. **`router.py::_log_call`** does both, in order, and keeps its
   never-fail-the-request guard around both:

   ```python
   await self._store.record(call)
   await self._learner.observe(call)
   ```

   `Router.__init__` takes `learner: Learner | None`.

3. **`broker.py`**: `self._store` is the real store; delete `_base_store` and
   every `effective_store` alias. `_require_queryable` keeps only the protocol
   check and loses its explanatory comment — the question is now honest.
   `record_quality` writes to the store, then calls the learner.

4. **`learning.py::resolve_metrics_map`** is deleted. `PoolView` and `AsyncLLM`
   take a `Callable[[], Awaitable[dict[str, LLMMetrics]]]` supplied by the
   broker: the learner's cache when there is one, a tail read when the store is
   queryable, `{}` otherwise. The isinstance branch that inspected the store
   goes with it.

## Part 2 — budget evidence from the journal

5. **Journal carries the missed budget explicitly.** Add a nullable
   `budget_ms: int` column to the `calls` table in `backends/spec.py` and a
   `budget_ms: int | None` field to `Call`. Bump `SCHEMA_VERSION` 5 → 6.
   A bump is a supported event (`decisions.md::no-schema-migrations`:
   create-fresh, fail-fast on mismatch) and this is what it is for.

   Do **not** derive the bound by matching `error_detail` text. Prose is not a
   contract, and invariant 20's spirit applies inside the library too.

6. **`router.py::_dispose`** sets `budget_ms` on the row it already writes when
   the verdict's outcome is `_BudgetExpired`. Nothing else changes: the attempt
   is still not cooled and still not counted as failing.

7. **`pool.py`** — delete `note_unmet_budget`, `clear_unmet_budget`,
   `_over_budget`, both `_Slot` fields and both constants. Add:

   ```python
   async def apply_budget_bounds(self, bounds: dict[str, float]) -> None
   ```

   Replaces the map wholesale, like `load_scores` does for quality windows —
   a rebuild is a fresh derivation, not an increment. `acquire`'s sort key keeps
   its first term, now reading the applied map.

   Keep the one-second slack as the comparison's own constant in `pool.py`: it
   is a property of comparing budgets, not of the evidence.

8. **`learning.py::_apply_peer_effects`** derives the bound alongside the
   cooldowns it already collects from the same rows: per model, the largest
   `budget_ms` among rows inside the rebuild window, converted to seconds. The
   window is the rebuild's tail read — the 10-minute expiry disappears, since a
   bound now ages out of the tail exactly as a cooldown does.

9. **`_finish_ok`** drops its `clear_unmet_budget` call: a success in the tail
   is already what stops the bound being derived. Confirm that with a test
   rather than by reading — this is the one place the two mechanisms differ in
   timing.

## Tests

- `tests/test_budget_ordering.py` (263 lines) is rewritten against the journal:
  seed rows carrying `budget_ms`, run a rebuild, assert the ordering. The
  behavioral assertions — deprioritized not excluded, budget-relative, cleared
  by a success — are kept verbatim; only the arrangement changes.
- `tests/test_learning.py`: `Learner` observed via `observe()` directly, no
  store wrapping.
- A test that `isinstance(store, QueryableStoreProtocol)` is now false for a
  non-queryable store even with learning enabled — the lie this plan removes.
- `tests/test_driver_conformance.py` covers the new column on all three
  backends.

## Spec updates

- `decisions.md::budget-expiry-teaches-ordering` — restate the current shape:
  the bound is derived from the journal with everything else. Describe the
  shape, not the move; no "used to live in the pool".
- `rules/journal.md` — the journal now carries the missed budget. One line.
- `rules/selection.md` — if it names a 10-minute unmet window, that window is
  gone; the bound now ages with the rebuild tail.
- `invariants.md` — no new entry. Invariant 8 already covers this; this plan
  makes the code obey it.

## Gate

`invoke pre` clean, `python -m pytest` green. Docker running — the schema bump
touches all three backends.

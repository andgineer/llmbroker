# Observed latency enters the fallback order

**Ships in one release with [`stream-stall-timeout.md`](stream-stall-timeout.md).** On its
own this plan learns only about the wait for the first token; the trickle after it is the
other plan's job, and the headline case in the evidence is a trickle.

Source: [`latency-aware-fallback.md`](latency-aware-fallback.md), "Plan 1" (it absorbs the
draft's former items 2a and 2b).

## Goal

A caller with a tight budget must not be handed the slowest endpoint in the pool as its
*first* fallback. The pool's only latency evidence today is a budget a model **missed**, and
a model that answers slowly never misses one — it answers, successfully, taking a minute
about it. This plan adds a second number, derived from the answers a model actually gave,
and lets it raise the same budget-relative ordering term the miss bound raises. Ordering
only, budget-relative, never a withdrawal.

## The four decisions the draft left open

**1. The statistic is a median over the last 10 observations, counted from the third.**
A median is the coarse default. Ten keeps the per-model state trivial and lets a model that
got faster wash its history out within a handful of answers. The floor of three is what
stops one slow answer — a provider hiccup, a cold start — from reordering the pool, and it
is the smallest count at which a median is a median rather than the mean of two. Both live
in `pool.py` beside `BUDGET_BOUND_WINDOW_SEC`.

**2. The observations have no clock window.** The miss window exists for a reason that does
not transfer: a miss must expire on its own, because a model kept out of first place
produces no successful rows and nothing else would ever clear it. An observed latency has
the opposite property — it is *produced* by success, so any answer the model gives refreshes
it, including the ones it gives as a last-resort candidate or to a caller with no budget. A
ten-minute clock would instead delete precisely the evidence about the untrafficked
fallbacks this plan exists to order. So the samples live exactly as long as the pool holds
them: appended live, and replaced wholesale from the one journal-tail read at each rebuild,
which is the quality windows' lifecycle exactly. Nothing new is invented, and a model whose
rows have fallen off the tail loses its number at the next rebuild.

**3. The two numbers stay two, compared as the larger.** The miss bound keeps its semantics
untouched, its window included. At acquisition the term compares the caller's remaining
budget against `max(miss bound, observed median)`, with the existing slack. Deriving one
blended bound would put success-retires-miss back on top of evidence that success produces.

**4. A row contributes its first-delta time when it carries one, its whole time otherwise.**
One rule covers every case. A completion has no first-delta time and its whole time is
honest — no consumer stands between the model and the row. A stream that opened contributes
the wait for its first delta, which no consumer can move, whether the answer then ran to the
end or was abandoned. The one residual is a stream abandoned *before* any delta: it journals
`OK` with a short whole time and would read as fast. It is rare, it is one sample, and the
median-with-a-floor-of-three in decision 1 is what absorbs it — which is the second reason
that floor is there.

**Deviation from the draft.** The draft's "`_finish_ok` stops clearing what it should not"
needs no code. With two numbers, success clears the miss bound exactly as it does today, and
nothing clears the observed latency because nothing was written to clear it. Decision 4
disposes of the abandonment case on its own. No clearing behaviour changes anywhere.

## Work order

Both gate commands are green at the end of every batch: `. ./activate.sh`, then
`invoke pre` and `python -m pytest`.

### Batch 1 — the journal carries the time to the first delta

- `models.py`: `Call` gains `first_delta_ms: int | None = None`, after `budget_ms`.
- `backends/spec.py`: `"first_delta_ms": "int"` in `TABLES["calls"].columns`;
  `SCHEMA_VERSION` 7 → 8. The three drivers map portable types themselves and need no edit.
- `backends/ports.py`: `_call_to_row` writes it; `_row_to_call` reads it back.
- `standalone/store.py`: `_call_from_jsonable` reads it. The write side is `asdict` and
  needs nothing.
- `broker/router.py`: `_StreamProgress` gains a monotonic `first_delta_at`, stamped in
  `opened()`; `_record` and `_finish_ok` each gain a `first_delta_ms` keyword; both
  `_finish_ok` calls in `_stream_attempt` — the clean end and the `GeneratorExit` — pass
  `progress.first_delta_at` measured against `attempt.t0`.

**Only answered rows carry it.** A stream that died mid-answer journals its failure without
the field: that row cools the model and never feeds the number, so filling it in would be
schema for nothing.

### Batch 2 — the pool holds the second number

`broker/pool.py`:

- `LATENCY_SAMPLES = 10` and `LATENCY_MIN_SAMPLES = 3`, beside `BUDGET_BOUND_WINDOW_SEC`.
- `self._latency: dict[str, deque[float]]`, each `deque(maxlen=LATENCY_SAMPLES)`.
- `observe_latency(name, seconds)` — sync, appends, mirroring `raise_budget_bound`, which is
  sync for the same reason: the live path applies it without taking the condition lock.
- `async apply_latencies(observed: dict[str, list[float]])` — replaces wholesale under the
  lock, mirroring `apply_budget_bounds`. Values arrive oldest-first.
- `_observed_latency(name) -> float` — `median(samples)` once there are `LATENCY_MIN_SAMPLES`
  of them, `0.0` below that.
- `_over_budget` compares `remaining` against the larger of the live miss bound and the
  observed median, returning False when that larger value is `0.0`.
- `drop()` pops the samples, as it already pops the bound.

### Batch 3 — the live path and the rebuild feed it

- `broker/router.py`: `_finish_ok` calls `observe_latency` with the first-delta seconds when
  the stream opened, and with `time.monotonic() - attempt.t0` otherwise. Applied from the
  call itself, not at the next rebuild, for the reason `selection.md` already gives about
  the miss bound: rebuilds are rare and every caller until the next one would walk into the
  same wait.
- `broker/learning.py`: `observed_latencies_from_calls(rows) -> dict[str, list[float]]` —
  `OK` rows only, newest-first in and oldest-first out, at most `LATENCY_SAMPLES` per model,
  `first_delta_ms` preferred over `latency_ms`, rows carrying neither skipped. `relearn`
  hands the result to `apply_latencies` from the `rows` it has already read; no second read.

Another node's rows are taken, exactly as the miss bound takes them. That is a deliberate
difference from a cooldown (invariant 11), and `selection.md` already carries the reason.

### Batch 4 — the specs

Written in this batch, not swept up afterwards. See "Spec moves" below.

## Tests

New file `tests/test_observed_latency.py`, with a harness copied from
`test_budget_ordering.py` — do not refactor that file to share it.

The journal field:

- `test_a_stream_journals_the_time_to_its_first_delta`
- `test_a_completion_journals_no_first_delta`
- `test_an_abandoned_stream_journals_the_true_first_delta_not_the_abandon_time`
- `test_a_stream_that_died_mid_answer_journals_no_first_delta`

The number and the order it produces:

- `test_a_slow_model_sorts_after_a_fast_sibling_for_a_tight_budget`
- `test_two_slow_answers_do_not_reorder_the_pool` — the floor of three
- `test_the_median_absorbs_one_slow_outlier`
- `test_a_faster_model_washes_out_its_slow_history` — eviction at ten
- `test_a_success_does_not_erase_the_observed_latency` — the case that forced two numbers
- `test_the_miss_bound_and_the_observed_latency_combine_as_the_larger`
- `test_a_caller_without_a_budget_ignores_the_observed_latency`
- `test_a_comfortably_larger_budget_ignores_the_observed_latency`
- `test_when_every_candidate_is_slow_curated_order_stands`
- `test_the_observed_latency_never_withdraws_the_last_candidate`
- `test_the_observed_latency_applies_without_waiting_for_a_rebuild`

The rebuild:

- `test_a_rebuild_derives_the_latency_from_the_tail`
- `test_a_rebuild_replaces_the_live_samples_wholesale`
- `test_a_rebuild_prefers_the_first_delta_time_over_the_whole_time`
- `test_a_dropped_slot_does_not_carry_its_latency_into_a_re_add`

The fence:

- `test_observed_latency_does_not_enter_the_quality_window` — invariant 5, stated as a test
  because its violation is silent.

Existing files the new column reaches: `tests/test_driver_conformance.py` and
`tests/test_store_backends.py` round-trip it on every backend (Docker must be up for the
testcontainer ones); `tests/test_schema_migration.py` already covers the version bump
generically — confirm, do not extend.

## Spec moves

- **`rules/selection.md`** — the section "A budget expiry teaches ordering" becomes the
  section about both numbers. Its premise sentence ("a model that never answers produces no
  successful rows, so its latency cannot be measured any other way") is false after this
  change and is corrected here, not only in `decisions.md`. The four properties that keep
  the signal from being a penalty in disguise hold for both numbers and stay as they are.
  Add: what a stream contributes is the wait for its first delta and what a completion
  contributes is its whole time; the observations have no clock of their own and are
  replaced wholesale by a rebuild. **Name no field and no constant** — the rule is the
  behaviour, not the column.
- **`rules/selection.md`, quality demotion** — one sentence, the draft's former item 2b:
  quality learning reaches only the models that get traffic, so a fallback the primary never
  yields to keeps its curated position however diligently a host rates. It is why the
  ordering evidence has to be obtainable without traffic. One sentence, no more.
- **`invariants.md`, invariant 8** — its second clause is already narrower than the code
  (the miss bound and the snapshot metrics are derived from the journal too), and this
  change makes the drift plainly wrong. Replace the headline with **"The journal is the only
  durable state, and one read of its tail is all that is re-derived from it."** The body
  keeps every existing sentence about there being no second state subsystem and about live
  quality being reached two ways and only two; only the "quality is the only thing derived
  from it" claim goes. No new entry — the file is at its cap.
- **`decisions.md`** — one new entry and two amendments, verbatim below.

### decisions.md, verbatim

New, under "Learning and quality", after `budget-expiry-teaches-ordering`:

```markdown
### observed-latency-is-its-own-number

Latency read off successful rows is a second number beside the miss bound, not a
widening of it. Ordering compares the caller's remaining budget against the larger
of the two.

**Blocks:** widening the miss bound to take successful rows; a stream contributing
its whole-answer time; observed latency in the quality window (invariant 5).
**Why:** a model's own success retires every miss older than it, deliberately — so
evidence derived from successful rows would be erased by the rows that produce it,
and a slow model answers successfully every time. The whole-answer time on a stream
is taken when the row is written, which is after the consumer finished pulling or
abandoned the generator, so it measures the consumer as much as the model; an
abandoned slow stream would journal a short time and teach the pool that the model
is fast. What a stream contributes is the wait for its first delta, which no
consumer can move — the same split between the two paths that the miss bound
already makes.
```

Amended — `budget-expiry-teaches-ordering`, one clause of its **Why** only:

```markdown
**Why:** a model that never answers produces no successful rows, so the expiry is
the only latency evidence it leaves — but blaming a model for the caller's clock
would teach the broker that healthy models are failing. Ordering only,
budget-relative, and never a withdrawal. Latency from the answers a model *does*
give is a separate number
([`observed-latency-is-its-own-number`](#observed-latency-is-its-own-number)).
```

Amended — `no-bandit-machinery`, narrowed rather than opened:

```markdown
### no-bandit-machinery

**Blocks:** ε-exploration, usable-rate floors, auto-retirement, and a global speed
ranking independent of the caller's budget.
**Why:** a chronically failing model is already effectively disabled by exponential
cooldown; the only thing auto-removed is a dead key. Slowness that *succeeds* is
not reached by that reason — cooldown disables failure, and a model answering in 40
seconds never fails — so it is ordered against the caller's own budget instead
([`observed-latency-is-its-own-number`](#observed-latency-is-its-own-number)).
```

## What this plan does not do

- No per-model timeout knob anywhere (invariant 7, `latency-budget-per-call`).
- No latency in the quality window, and no synthetic rating of any kind (invariant 5).
- No global speed ranking: the number is only ever read against one caller's budget.
- Nothing after the first delta is learned here. That is the other plan.
- No version bump — the maintainer does it by hand.

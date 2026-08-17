# A stall timeout on a stream

**Ships in one release with [`observed-latency-ordering.md`](observed-latency-ordering.md).**
That plan learns the wait for the first token and is blind to what follows it; this one is
what reaches the trickle, which is the headline row of the evidence — a model that opens
promptly and then dribbles for 101 seconds against a 45-second budget.

Source: [`latency-aware-fallback.md`](latency-aware-fallback.md), "Plan 2".

## Goal

Let a caller bound the gap *between* deltas. `wait` stops binding at the first delta on
purpose, so today nothing between the caller and the provider limits an answer that has
started; the caller's only defence is to stop pulling, which tells the pool nothing. A stall
gives it a bound and turns the abandonment into evidence.

## The shape

**The value belongs to the call.** `stall: float | None = None`, alongside `wait`, on
`Broker.stream` → `LLMs.stream` → `LLMs._deltas` → `Router.stream` → `Router._stream_attempt`
→ `_stream_deltas`. Nothing per-model and nothing per-entry (invariant 7,
`latency-budget-per-call`). `None` is off; a non-positive value is rejected at `LLMs.stream`
with `ValueError`, because a zero gap aborts every stream instantly and can never be meant.

**The gap is armed after the consumer comes back, never before it goes away.** In
`_stream_deltas`, today's `bound.reschedule(None)` at the first delta stays as it is, and the
stall deadline is armed **after `yield delta` returns** — that is, once the consumer has asked
for more:

```python
if not progress.started:
    progress.opened()
    bound.reschedule(None)
yield delta
if stall is not None:
    bound.reschedule(asyncio.get_running_loop().time() + stall)
```

Arming it before the `yield` would put the consumer's own processing inside the deadline and
reintroduce exactly the coupling that dropping the absolute deadline removed. This one line's
position is the whole correctness of the feature, and its violation is silent: everything
still works, and slow consumers start losing answers.

A chunk carrying no text does not re-arm anything — the clock runs to the next *text* delta,
so a provider emitting keepalives and nothing else stalls, which is the intent.

**A stall is a miss, not a failure.** When the deadline fires, `_stream_deltas` raises a
private marker exception, `_stream_attempt` catches it ahead of `_FAILOVER_ERRORS`, and the
attempt is settled through the existing `_dispose` with a verdict carrying no `cool_base` and
a `_BudgetExpired` outcome, `timeout` being the **elapsed** seconds since the attempt started.
That journals the row with the budget it did not finish within and raises the miss bound in
the pool — the same disposal a pre-first-delta expiry already gets, which is what makes the
slowness reach the ordering term of the other plan. The outcome is never read, because there
is no failover past the first delta (invariant 18): the call ends by raising.

**Why a private marker and not a flag.** `TimeoutError` subclasses `OSError`, so it is already
inside `_FAILOVER_ERRORS`. Without a type of its own it lands in `_fail_stream` with
`started=True`, is classified as a generic provider failure, **cools the model** and surfaces
as `StreamInterruptedError` — one caller's knob withdrawing a model from every other caller.
Distinguishing it by `progress.started` alone would work today and break the moment anything
else inside that block raises a timeout.

**A new exception type.** `StreamStalledError(LLMRequestError)` in `exceptions.py`, carrying
the model name and the elapsed seconds, re-exported from the top-level package and its
`__all__` — a host catches it, so it belongs there. Not a subclass of
`StreamInterruptedError`: "the stream died" and "my own gap fired" are different states, and a
host that wants both catches `LLMRequestError` (invariant 20). The deltas already yielded
stand, exactly as they do on an interruption.

## Work order

Both gate commands green at the end of each batch: `. ./activate.sh`, then `invoke pre` and
`python -m pytest`.

1. **The exception.** `exceptions.py` and the top-level `__init__.py` re-export plus
   `__all__`. `tests/test_exceptions_surface.py` covers this surface — extend it.
2. **The knob and the clock.** The signature chain above, and the reschedule in
   `_stream_deltas`. With `stall=None` every existing streaming test must pass untouched;
   that is the batch's own check.
3. **The disposal.** The private marker, the catch in `_stream_attempt` ahead of
   `_FAILOVER_ERRORS`, the `_dispose` call with the elapsed seconds, the raise.
4. **Specs and docs**, in this batch and not after it.

## Tests

New file `tests/test_stream_stall.py`.

- `test_a_stall_fires_when_the_gap_between_deltas_is_too_long`
- `test_a_slow_consumer_never_trips_the_stall` — the load-bearing one: a provider emitting
  promptly while the consumer sleeps far longer than the stall must not fire it
- `test_a_stall_does_not_cool_the_model`
- `test_a_stall_journals_the_time_it_did_not_finish_within`
- `test_a_stall_raises_its_own_exception_not_stream_interrupted`
- `test_the_deltas_already_yielded_stand`
- `test_a_stall_does_not_bound_the_wait_for_the_first_delta` — `wait` still owns that
- `test_a_keepalive_chunk_carrying_no_text_does_not_rearm_the_gap`
- `test_a_non_positive_stall_is_refused`
- `test_no_stall_leaves_streaming_exactly_as_it_was`
- `test_a_stalled_model_sorts_after_its_faster_sibling_afterwards` — the join with
  [`observed-latency-ordering.md`](observed-latency-ordering.md), and the reason the two ship
  together

`tests/test_router_stream.py` and `tests/test_wait_budget.py` are the existing neighbours;
they should need no edit, and needing one is a signal worth reporting.

## Spec moves

- **`rules/call-path.md`** — the streaming section gains the stall: a caller may bound the
  gap between deltas; the clock runs only while the library waits on the provider and never
  while the consumer holds the generator; a stall never cools the model, is recorded as a
  budget the model did not finish within, and ends the call with its own exception. Failover
  past the first delta stays impossible (invariant 18, unchanged). No constant and no field
  name in the prose.
- **`decisions.md`** — one new entry, verbatim below, under "The call path" beside
  `latency-budget-per-call`.
- **`docs/src/en/usage.md`** and **`docs/src/ru/usage.md`** — the streaming section gains the
  knob and the sentence that it bounds the gap, not the answer.

### decisions.md, verbatim

```markdown
### a-stall-is-a-gap-not-a-deadline

A streaming caller may bound the gap between deltas. It is journaled as a budget the
model did not finish within, it never cools the model, and it raises its own
exception.

**Blocks:** an absolute deadline on the whole answer; a stall belonging to the model
or the entry rather than to the call; reusing the mid-stream-death exception for it;
cooling a model because a caller's gap expired.
**Why:** an absolute deadline is what was dropped so a slow consumer could not trip
it, and a gap keeps that intact — the clock is armed only while the library waits on
the provider, never while the consumer holds the generator. The model did answer, so
cooling it would take it from every other caller over one caller's knob; what it did
not do is finish, which is what the miss bound already records. And a host must tell
"the stream died" from "my own gap fired" (invariant 20), so the two are different
types.
```

## What this plan does not do

- No default stall. Unset is off, and the library bounds nothing it was not asked to.
- No stall on `ask`/`chat` — `wait` already bounds a completion end to end.
- No failover after a stall (invariant 18).
- No version bump — the maintainer does it by hand.

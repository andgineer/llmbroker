# Plan — a budget that no cooldown can meet ends at once

**Status: source-bound on `7daec8c3c`.** One batch: one check in slot acquisition, its
tests, one spec paragraph, one `decisions.md` entry, two doc passages.

**Queue position:** third, after [`host-gaps.md`](host-gaps.md) and before
[`architecture-simplification.md`](architecture-simplification.md). Host gaps item 1 bounds
the pass that follows a key re-read by the caller's own deadline; this plan makes that pass
reachable at the start of a budget instead of at its end, so it is implemented on top of
that handover. The refactor rewrites deadlines, so its revalidation reads this handover.

## The defect

**Now.** `LLMPool.acquire_many` (`broker/pool.py`) loops: reserve what is free; raise when
there are no candidates; raise `reason="timeout"` once `queue_deadline` has passed; otherwise
sleep on the condition for `_wake_timeout(...)` — the smaller of the nearest cooldown expiry
and the time left to the deadline.

When every candidate is cooling and the nearest expiry lies past the deadline, the sleep is
the whole remaining budget and the outcome is fixed before it starts: a cooldown carries the
instant it ends and nothing shortens it, except a call still in flight on that model
answering (`clear_cooling`). The caller is held for `wait` seconds and then given exactly
the error — `NoLLMAvailableError`, `reason="timeout"`, `retry_at` — that was already known.

Both call shapes go through this loop: a completion through `Router._acquire`
(`broker/router.py`), a stream through `RoutedStream._acquire` (`broker/stream_owner.py`).
A stream that has already tried its models ends at once on a cooling pool; a stream or a
completion that has tried nothing sleeps the budget out.

**Unaffected.** `wait=None` has no deadline and keeps waiting for the cooldown. `wait=0`
and a negative `wait` already raise without sleeping.

## Do

In `acquire_many`, between the "no candidates" check and the sleep, raise the timeout error
when the wait is futile, alongside the existing "deadline passed" condition:

```python
def _wait_is_futile(self, candidates: list[_Slot], now: datetime, queue_deadline: float) -> bool:
    left = queue_deadline - time.monotonic()
    return all(
        s.in_flight == 0
        and s.cooldown_until is not None
        and (s.cooldown_until - now).total_seconds() > left
        for s in candidates
    )
```

- Futile means **every** candidate is cooling past the deadline **and** none has a call in
  flight. One candidate that is merely busy (at its `parallel` cap, or a recovery another
  caller holds) or that has an in-flight call keeps today's wait: a release or an answer can
  free it inside the budget.
- The comparison is strict. A cooldown ending exactly on the deadline is not futile; the
  loop's existing deadline check ends that call.
- The raised error is the existing one: same type, `reason="timeout"`, `retry_at` from
  `_earliest_return`. No new reason value, no new exception. The message for the early exit
  says what happened: `"no LLM returns from its cooldown within wait"`.
- Re-evaluated on every loop iteration, as the reserve is, so a pool that becomes all-cooling
  while a caller queues (the last free model cooled by a sibling call, or by this call's own
  failover) ends the call then, not at the deadline.
- Nothing else moves. `AsyncLLMs.chat` (`broker/llms.py`) already catches the error, lets
  the broker re-read its ports and runs its second pass; that now happens at the start of
  the budget. `_wake_timeout` is unchanged.

## The decision this records

Lands in `specs/reference/decisions.md` under "The call path", in this batch:

> ### a-budget-is-not-slept-out-for-a-known-outcome
>
> **Blocks:** holding a caller for its whole `wait` when every model it could use is
> cooling past that deadline; a separate reason value for that early exit; ending early
> on a *prediction* — that a model returning shortly before the deadline has no time left
> to answer; waiting past the budget for the cooldown; and sending to a cooling model
> because the caller cannot wait for it.
> **Why:** a cooldown carries the instant it ends and nothing shortens it but that model
> answering a call already in flight, so with no such call the outcome of the wait is a
> fact, not a forecast. Sleeping it out buys nothing and charges the caller the pool
> exists beside — the host with a fallback — its whole budget on every call of an outage,
> which is exactly when the fallback is needed. The answer is the one the budget's expiry
> would have given, `timeout` with `retry_at`, because the host's question is the same:
> come back later, or fall back now. A stream is already told this the same way
> ([`a-stream-reports-what-can-come-back`](#a-stream-reports-what-can-come-back)). The
> line is drawn at proof: how long a model takes to answer is not known the way its
> cooldown is, so a model that returns inside the budget is always waited for.
> **Accepted cost:** a model or a key that a re-read would have brought in during the
> budget is not waited for; the exhaustion itself triggers that re-read, at once rather
> than at the deadline. And a host that retries in a tight loop without reading
> `retry_at` is no longer slowed by the sleep, as it already is not with a zero budget.

## Tests

Each new test fails before the fix. Lower bounds on measured spans use `CLOCK_SLACK`
(`tests/support.py`); "at once" is an upper bound well under the budget and needs no slack.

`tests/test_pool.py`, beside `test_waiter_wakes_on_cooldown_expiry_with_inflight_sibling`:
- one model cooling for 30 s, deadline 2 s out: `acquire` raises `reason="timeout"` with
  `retry_at` equal to the cooldown's end, in well under the budget;
- two models cooling for 30 s and 0.1 s, deadline 2 s out: the call waits and is given the
  second — the nearest expiry inside the budget is waited for;
- one model cooling for 30 s with `in_flight` at 1, deadline 0.3 s out: the call sleeps the
  budget out (lower bound `0.3 - CLOCK_SLACK`) and then raises — an in-flight call keeps the
  wait; `clear_cooling` plus `release` during that wait hands the slot over;
- one model cooling for 30 s and a second, not cooling, held at `parallel=1`: the call
  waits, and a `release` of the second serves it;
- a caller queued behind a busy `parallel=1` model with 2 s left; the holder's call ends in
  `cool_down(…, 30)`: the queued caller raises at that moment, not at its deadline;
- `queue_deadline=None` on an all-cooling pool still waits for the expiry
  (`test_acquire_wait_none_wakes_when_cooldown_expires_without_a_timer` stays green).

`tests/test_wait_budget.py`:
- `Router.chat(wait=5)` on a pool whose only model answers 429 with `Retry-After: 30`:
  raises `reason="timeout"`, `retry_at` set, in well under the budget, after exactly one
  provider call;
- the same pool, a second `chat(wait=5)` made afterwards: raises at once with no provider
  call at all.

`tests/test_router_stream.py`, beside
`test_a_cooling_pool_ends_a_stream_in_timeout_naming_when_it_is_back`: a stream opened with
a positive `wait` on a pool already cooling past it ends at once with the same pair, having
opened no request.

`tests/test_rebuild_triggers.py`, beside
`test_a_key_stored_while_the_pool_is_exhausted_answers_the_same_call`: a positive `wait`, the
only keyed model cooling past it, a key for a second model landing before the call: the call
answers from the second model at once — the re-read ran at the start of the budget.

Existing tests that asserted a full-budget sleep on an all-cooling pool with nothing in
flight are asserting the defect; re-point them at the new behavior rather than loosening
them. `test_cooldown_expiry_alone_does_not_admit_a_slot_at_capacity` is not one of them:
its model has a call in flight.

## Specs and docs

- `specs/reference/rules/call-path.md`, the `wait` section, after the three bullets: a
  positive budget is spent only on a wait that can end inside it; where every model the call
  could use is cooling past the deadline and none is answering another call, the call ends
  at once with the answer the expiry would have given. Link to the entry; state no
  mechanism.
- `specs/reference/decisions.md`: the entry above, verbatim.
- `docs/src/en/usage.md` ("When nobody can answer") and `docs/src/en/async.md` (the budget
  section): both say `ask` keeps waiting for a model to come back where a stream ends at
  once. Add that with a `wait` shorter than the earliest return, `ask` ends at once too,
  with the same `timeout` and `retry_at`, and that a host retrying should wait until
  `retry_at`.
- Not `invariants.md`: the rule is local to the call path and its violation is not silent.
- Not `mission.md`: nothing there predicts a caller being held for a known outcome.

## Out of scope

- Any use of learned latency or the miss bound to end a call early.
- Any change to `wait=None`, `wait=0`, a negative `wait`, or to when the re-read fires.
- A way for a host to ask for the old sleep. `retry_at` is that way.

## Gate

`. ./activate.sh`, then `invoke pre` and `invoke test`, and `invoke downstream` once at the
end. No version bump, no commit.

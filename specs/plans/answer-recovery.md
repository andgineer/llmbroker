# Plan — another complete answer from the same pool call

**Status: source-bound on `b912317d1`; not implemented.** Requested by echo-words on 2026-09-08
for part D of its
[`answer-recovery.md`](../../../echo-words/spec/plan/answer-recovery.md#d-another-the-pools-next-answer).
The public shape comes from that agreed design; the implementation binding below
uses the current routing, receipts and tests.

**Dependencies: none of the other queued plans.** `AsyncResult`, `CallReceipt`,
stream usage and per-call ratings already exist. `caller-visibility.md` extends
observability and `load-harness.md` supplies an optional measurement tool; neither
supplies a missing runtime primitive. This plan is first and can be implemented
independently. Revalidate the harness against its handover afterwards.

## Goal and evidence

A host can reject a completed answer on its own content rules, then obtain the
next complete answer from the same routed request. It can use the work of lanes
already running and then try remaining pool candidates, without selecting model
names, restarting the request's budget or buying a paid answer inside llmbroker.

The motivating production request was German `allein` with context. On
2026-09-08 at 05:08:00 UTC, the first pool answer finished in 2.007 s; the other
lane was journaled as superseded at the same instant. The host rejected the
winner's payload after the race had closed and entered a paid step that produced
no visible output. Across the host's fifteen recorded payload rejections,
fourteen contained content its reader could have accepted. Those are the host's
recorded observations, not a controlled measurement of this proposed API.

Local repair, context matching, page preservation and the paid-step decision
belong to echo-words (parts A-C). This plan supplies the pool continuation only.
The broker neither parses the host's payload nor decides that an answer is bad.

## Caller contract

The streamed handle gains `await stream.another() -> AsyncResult | None`.
This is the agreed public shape, not a choice left to implementation. The usage
boundary includes the host's validation and every continuation:

```python
async with aclosing(broker.stream(prompt, fastest_of=3, wait=25)) as stream:
    try:
        text = "".join([delta async for delta in stream])
        rated = stream
    except StreamReplacementError as exc:
        text, rated = exc.replacement.text, exc.replacement
    while not acceptable(text):  # The host owns this predicate and its scores.
        await rated.record_quality(0.0)
        answer = await stream.another()
        if answer is None:
            break
        text, rated = answer.text, answer
```

- No opt-in flag. Finishing the first answer, including the typed replacement
  path, ends delta iteration but leaves the handle open for continuation.
- A sibling is not cancelled merely because another lane completed first.
  Already-open lanes continue draining independently of the consumer, bounded by
  the original call budget. Closing the handle ends the work it still owns.
- `another()` returns each additional complete answer once, in provider
  completion order. It first consumes the call's existing lanes, including
  completed answers held in memory, then asks models this call has not tried.
  A slower journal write must not reorder provider completions.
- The first returned or replacement answer is already consumed; `another()`
  must never return it again. It returns a complete result, never more deltas
  and never a second `StreamReplacementError`.
- Continuation works with every valid race width, including one, an omitted
  width and a pool smaller than the requested width. Ordinary streams keep
  their first-delta commitment while producing their initial answer.
- After the first answer, retaining a handle starts no new provider request by
  itself. Existing lanes may finish; a new candidate requires `another()`.
  Before that boundary, ordinary failure refill remains available.
- Every continuation sends the original request under the original caller's
  keys, scope, operation and trace. There is no validation callback, repair
  prompt, retry prompt or model-name exclusion list for the host to manage.
- `None` means no further complete answer is obtainable within this call's
  candidate and waiting constraints. Buffered answers completed within budget
  remain retrievable after its deadline. Once exhausted, later calls return
  `None` without acquisition or provider work.
- Existing initial-stream error contracts remain in force when no first answer
  exists. During continuation, classified attempt failures are absorbed while
  candidates remain; expected pool exhaustion, including timeout, becomes
  `None`. Cancellation and non-exhaustion faults are not disguised as exhaustion.

## Ownership, budget and settlement requirements

The handle must own continuation beyond the lifetime of its delta iterator.
An async generator's normal exhaustion or replacement exception runs its
`finally` blocks; merely adding a method to that generator cannot retain lanes.
Explicit `aclose()` is idempotent and waits for all owned settlement to finish.
Closing before iteration must not start a request. Closing during a continuation
or cancelling its await must release every acquired slot and leave no orphan task.

A positive `wait` is one budget shared by initial answer and continuations.
Neither `another()` nor a new candidate grants a fresh budget. Existing special
meanings of `wait=None`, zero and negative values still apply. Preserve ordinary
streams' consumer-time exclusion while their initial deltas are pulled: extend
the shared deadline by those consumer pauses, not just the attempt-local timer.
Explicit races keep draining against their one deadline, so consumer pauses do
not extend it. After the first complete answer, the remaining deadline runs
continuously, including validation, buffered replay and pauses between calls to
`another()`. Completed answers remain readable; starting new provider work after
expiry is forbidden. No extra timeout knob is introduced. The no-budget case
must still terminate by candidate exhaustion.

Track attempted models across the entire continuation, including failed attempts,
completed answers and recovery lanes. Do not retry a model after its cooldown
expires in the same continuation. Busy or cooling untried candidates use existing
acquisition and `wait` rules once no existing lane can answer. An empty
opportunistic refill is not proof that the whole pool is exhausted. The sequence
is bounded by the call's eligible candidate set; a pool refresh must not turn a
live handle into an endless source of newly admitted names.

Each completed answer has its own immutable model identity, call id and usage,
and is rateable only after its own call row is written. The stream continues to
name its initial authoritative answer; requesting another does not retarget a
previous receipt. The host rates each returned result separately. No synthetic
rating follows from requesting another, and no rating is fanned out by trace.

Every provider completion keeps its answered row, including an answer the host
never requests. Each real failure keeps its own classification and learning.
Closing an unfinished hidden lane settles it neutrally as superseded; a lane
that never reached its provider writes no call row. Keep the initial stream's
abandonment contract for the visible lane when no complete answer exists.
Settlement already under way must finish without double release or rewritten rows.

## Decision entry to land with the implementation

The existing
[`parallelism-is-explicit-or-recovery-owned`](../reference/decisions.md#parallelism-is-explicit-or-recovery-owned)
decision still governs parallelism and the first answer. The cancellation-at-first-
completion rule in `rules/call-path.md` must change for streamed handles. Add this
entry verbatim when that behavior lands; do not publish it as current state now:

> ### streamed-alternatives-live-until-close
>
> A streamed handle retains already-open alternatives after its first complete
> answer. The host may request another complete answer, and closing the handle
> ends the provider work still owned by it. Only an explicit request for another
> answer starts further candidates after the first answer exists.
>
> **Blocks:** cancelling every alternative on the first completion; a retention
> flag; host-side rerouting by model name; automatic content validation or a paid
> fallback inside the broker.
> **Why:** only the host can judge the payload, and its judgement follows the
> completion that cancellation would make irreversible. One request already owns
> distinct candidates, their budget and their attribution; another ordinary pool
> call owns none of that history. Explicit close supplies the lifetime boundary.
> **Accepted cost:** reserve lanes may consume the rest of their output quota,
> occupy slots and retain buffered answers until they finish, reach the budget or
> are closed. Slow hosts can make this cost material; first-completion cancellation
> saves that work but destroys the very alternatives the host needs to inspect.

## Implementation binding and work order

Paths below are relative to `src/llmbroker/`. Implement the ownership and routing
changes as one coherent batch with their tests and reference edits; do not expose
`another()` while generator cleanup still destroys its alternatives.

1. **Make stream lifetime explicit.** In `broker/router.py`, introduce a private
   routed-stream owner holding the request, `_Call`, lanes and delivered-answer
   identities. `Router.stream()` constructs it lazily; its initial delta iterator,
   `another()` and `aclose()` operate on that same owner. Move final cleanup from
   normal `_run_race` completion to the owner's close. Exceptional initial failure
   and cancellation still clean up. `_retire` must cease cancelling retained
   stream lanes at provider completion; keep atomic `_race` settlement unchanged.
2. **Wire the existing public surfaces.** `broker/result.py` keeps `StreamHandle`
   and adds its async `another()` method. Supply private delegation callbacks for
   continuation and close alongside its initial iterator, avoiding a result-to-
   router import cycle. Guard active pulls in `__anext__` and return the handle
   from `__aiter__`; no second iterator or concurrent continuation is supported.
   `broker/llms.py` owns lazy provisioning and delegates to the routed-stream
   owner. Its `_deltas` must not close that owner on normal completion or
   `StreamReplacementError`. Keep `AsyncBroker.stream()` and scoped
   `AsyncLLMs.stream()` signatures unchanged; both return this handle.
3. **Preserve initial routing and retain alternatives.** Reuse `_StreamRace`,
   `_StreamLane`, `_drain_lane` and provider completion timestamps for explicit
   races. Keep ordinary first-delta commitment, including pool-owned recovery:
   drain retained recovery siblings privately without letting one replace the
   committed initial text. The ordinary visible lane stays consumer-driven so
   its pause accounting survives. An ordinary width-one call starts no background
   reserve. All stream paths use the same owner and attempted-model history.
4. **Continue in completion order.** Mark the initial authoritative answer
   delivered before ending iteration or raising its replacement. Scan remaining
   completed lanes by `(completed_at, opening_order)` and await the selected
   lane's settlement before constructing its `AsyncResult` with `_replacement`'s
   existing attribution logic. Waiting on a slower journal must not yield a later
   completion first. Drain existing lanes before opening a new wave. While an
   `another()` await needs an answer, refill failed lanes up to the existing
   effective width; stop starting candidates as soon as an answer is available.
   Surviving lanes remain owned for the next await or explicit close.
5. **Make exclusions call-wide.** Add a monotonic attempted-name set to the
   stream owner and use it in `_acquire`, `_untried` and both stream refill paths.
   Mark a reservation before opening its provider, including recovery lanes.
   The current `_Call.client_failed` covers request-local exclusions, not every
   attempted model; the race-local lane list does not survive outer reacquisition.
   Capture pool membership after lazy provisioning. Add an optional internal
   eligible-name filter to `LLMPool.acquire_many` and `take_free`, checked inside
   their condition-protected candidate selection on every wake; other callers
   retain their default unrestricted selection. Recompute payable keys and live
   availability normally. Only the existing one-time pre-output `_on_exhausted`
   refresh may extend captured membership, once, retaining attempted names and
   the original budget. No such refresh is triggered by `another()`.
6. **Carry the clock and terminal state.** Implement the budget rule above in
   `_stream_deltas` and the owner: ordinary consumer pauses must update shared
   remaining time as well as the current provider's timer. Do not retain the
   `_deltas` retry's `wait=0` as a way to discard an already-spent positive budget.
   The existing no-queue meaning remains for calls originally made with zero.
   Check buffered completions before expiry and exhaustion. Once continuation
   has no viable candidate, return `None` for `NoLLMAvailableError` or a spent
   budget; retain the current actionable last client `ProviderError` precedence.
   If an unexpected lane fault remains and no answer can be obtained, raise the
   earliest such fault in lane opening order before a generic exhaustion result.
   After a terminal error, close the owner and propagate it rather than leaving
   an awaitable that retries the same fault.
7. **Pin lifecycle misuse and ratings.** `another()` before a complete initial
   answer (including an abandoned or failed initial iterator), after close, or
   during another active pull/continuation raises `RuntimeError` before provider
   work. Normal exhaustion stays open and returns repeatable `None`. `aclose()`
   may interrupt an active pull or continuation: cancel that operation, await
   cleanup and propagate cancellation to its waiter. Repeated close awaits the
   same cleanup; cancellation of a waiter must not cancel settlement already
   writing a row. Keep `_close_race`/`_settled`'s exactly-once release safeguards.
   The handle's receipt stays attached to the initial authoritative answer;
   each additional result receives its own settled receipt and quality callback.

### Test placement

Reuse the event-gated SSE providers, fake transport and recording stores already
used by the streaming suites. New shared fixtures belong in `tests/support.py`
only when more than one suite needs them; do not import helpers from test modules.

| file | binding for the acceptance cases below |
|---|---|
| `tests/test_router_stream.py` | public `another()`, lazy start, scoped callers, initial refresh, lifecycle misuse and unchanged initial receipt |
| `tests/test_race.py` | retained lanes, widths, completion order, refills, recovery commitment, close/cancel and blocked settlement |
| `tests/test_whole_answer_budget.py` | shared deadline across continuations, ordinary pauses, raced pauses, validation delay and buffered replay |
| `tests/test_wait_budget.py` and `tests/test_pool.py` | zero/None/negative waits, busy candidates, finite exclusions and membership filtering across a pool refresh |
| `tests/test_rating_by_call.py` | independent ratings and usage, append order, correct scope and no journal reads required to rate |

Update the two stream-specific assertions in `tests/test_race.py` named
`test_a_losing_lane_leaves_its_provider_before_the_winners_row_lands` and
`test_a_losing_lane_is_retired_while_the_reader_holds_a_delta`: their cancellation
boundary becomes explicit close, and new tests prove retention before it. Keep
atomic-race cancellation assertions. Update internal stream helpers and existing
consumers in tests to use `aclosing` where they now retain owned alternatives.

## Acceptance evidence

Use event-gated fake providers and stores, not wall-clock races or real API keys.
The tests must demonstrate all of these externally observable outcomes:

- Width three: reject the first answer, receive the other two in completion order,
  then receive an untried model's answer; no model opens twice. Repeat with width
  one, omitted width and a pool narrower than requested.
- Both normal iteration and a typed first-answer replacement retain alternatives.
  A slow consumer and a slow first completion's journal preserve completion order.
- Accept and close the first answer: unfinished reserves are cancelled promptly,
  completed ones keep their rows, and no fresh candidate starts.
- Exhaustion is finite even without a positive budget, across failure refill,
  outer acquisition, cooldown recovery and a concurrent pool refresh. A temporarily
  busy untried candidate is treated according to the original waiting contract.
- A positive budget is not renewed; zero never queues; a negative budget opens
  nothing. A completed buffered answer survives deadline expiry. Test consumer
  pauses and continued reserve generation under the documented clock rule.
- Provider errors, silence, malformed/empty output and mid-answer failure do not
  hide a viable alternative. Expected exhaustion returns stable `None`;
  cancellation and unexpected errors follow their stated contracts.
- Close before start, after replacement, during `another()`, after exhaustion and
  during a blocked journal write: all slots return exactly once, all required rows
  land, no task remains. Repeated close is harmless.
- Rating the first and subsequent answers reaches distinct rows with correct
  identity, scope and usage; no receipt changes under a later result, no rating
  precedes its call, and merely requesting another teaches no quality score.
- Existing initial selection, provisional replacement, unraced commitment,
  atomic calls and direct clients retain their contracts outside this change.

## Documentation, measurement and gate

Update `reference/rules/call-path.md` with handle lifetime, continuation exhaustion,
budget and settlement rules; it must no longer promise exactly one answered row
or immediate cancellation for streamed alternatives. Land the decision entry above
in `reference/decisions.md`. Link existing invariants instead of duplicating them.
Update `docs/src/en/async.md`, its Russian counterpart `docs/src/ru/async.md`, and
the public docstrings in `broker/result.py`, `broker/llms.py` and `broker/broker.py`
with validation inside `aclosing`, both initial-answer paths, separate ratings
and explicit close. Keep in-repo additions in English per `CLAUDE.md`. No
application payload rules belong here.

The deterministic contract needs no live-model calls. The later `load-harness.md`
can measure the operational cost separately; this measurement does not block
implementing or accepting the API contract. Hold prompts, pool availability,
width, budget and pacing fixed; compare immediate close with rejection followed
by continuation. Record first-answer latency, time to an accepted answer, extra
provider requests and usage, candidate completion order and retained-lane duration.
Define the workload and secure operator approval for provider spend before running;
do not claim a recovery or latency improvement from fake providers. Semantic
acceptance of echo-words payloads and its required bench remain downstream work.

For implementation, activate the repository environment before each gate and run
`invoke pre`, then the full `python -m pytest`, with zero failures, errors or
skips. Keep the plan and append the implementation handover as required by
`CLAUDE.md`; no version bump, commit or publication is part of creating this plan.

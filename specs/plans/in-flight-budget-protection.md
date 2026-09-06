# Plan — make streaming `fastest_of` race complete answers

**Status: implementation-ready; runtime work has not started.** The behavior,
public surface, source ownership, settlement rules and test placement below are
settled against the current call path.

## Need

An interactive caller has one complete-answer budget and wants several eligible
models to work on the same request in parallel. The answer must still be selected
by speed, as `fastest_of` promises, but a streaming call must not confuse "first
model to emit one delta" with "first model to finish an answer".

The caller also prefers to begin by showing the pool's highest-ranked candidate
when that candidate starts within a small caller-chosen interval. That preference
is only about which provisional answer is shown while the race is unresolved. It
does not change the final `fastest_of` verdict: the first complete valid answer is
authoritative.

This protects the call in flight in both observed failure modes:

- a highest-ranked candidate may remain silent for the caller's whole budget while
  another candidate could answer;
- a lower-ranked candidate may emit a first delta immediately and then generate a
  poor or very slow answer, while the higher-ranked candidate finishes first.

## Why current streaming `fastest_of` is insufficient

All `fastest_of` candidates already start in parallel and share the caller's one
answer budget. The missing behavior is not delayed scheduling or a division of the
budget between models. It is settlement of a streaming race.

An atomic call naturally races complete valid answers. The current stream instead
commits permanently at the first useful delta and cancels every other lane. That
makes time to first token the final selection criterion, even though `wait` means
time to a complete answer. It also discards the only lane that could replace a
stream which later stalls or exceeds the budget.

Recovery protection remains a separate concern: it decides when the pool must
cover its own first post-cooldown recheck. The behavior here belongs to an explicit
`fastest_of` call and retains its ordinary quota cost.

## Evidence from an interactive host

The host has a 25-second complete-answer budget and streams a linguistic analysis.
With two-lane fastest-answer routing, a production-prompt smoke tier returned all
64 answers and had a 2.0-second median. The result was not an unconditional latency
or quality win:

- across 53 full answers, p90 was 11.7 seconds, p95 21.3 seconds and the maximum
  27.9 seconds;
- two Serbian answers committed to `openrouter-laguna-s-2.1` after its first delta
  at 0.7 and 1.2 seconds, then completed at 11.7 and 21.3 seconds;
- both answers were semantically unfit for the host, including corrupted forms, a
  missed learning unit and one explanation in the wrong target language.

The same host's earlier measurements show the opposite failure without ordinary
parallelism: a top choice can remain silent for the caller's whole budget, leaving
no time inside that call for a sequential fallback. The library learns from the
miss and improves later ordering, but that reader has already waited in vain.

Together these observations define the gap. The parallel work is useful; settling
a streaming race permanently on its first delta is the behavior that loses the
value of that work.

## Chosen functional behavior

### Every lane starts together

`fastest_of=N` reserves the ordinary top `N` distinct eligible candidates and
starts all of them at once, as it does today. They retain one shared caller budget;
there is no per-attempt slice and no delayed fallback request.

The pool's highest-ranked lane is the preferred lane. The other lanes are reserves
only in the sense that their output may initially be hidden; they are doing the
same provider work from the beginning.

### Selecting the provisional stream

`stream_selection_window`, expressed in seconds and defaulting to `1.0`, gives the
preferred lane a bounded opportunity to supply the stream's first useful delta.
It is meaningful only for a streaming call with more than one `fastest_of` lane.

- If the preferred lane produces a useful delta within that duration, it is
  streamed immediately. There is no reason to wait for a reserve once the lane
  which already has priority has begun.
- A reserve that produces deltas first is consumed and buffered internally. Its
  output is not yet shown while the preferred lane's interval remains.
- If the preferred lane begins within the interval, its output becomes the
  provisional stream and the reserve remains alive in the background.
- If the interval expires while the preferred lane is still silent, the earliest
  reserve output already buffered becomes the provisional stream.
- If every lane is still silent at expiry, the next lane to produce a useful delta
  becomes the provisional stream.

With more than two lanes, the bounded preference belongs only to the highest-ranked
lane. Reserves continue to compete by speed; this setting is not a second ranking
algorithm.

Expiry of the interval is neither an attempt timeout nor evidence about model
availability or answer latency. It does not cancel, cool, demote or teach anything
about the preferred lane. It only ends its privilege in selecting provisional
output.

The default comes from echo-words' interactive measurements. Across
`experiments/.bench/paced.jsonl`, `paced-152.jsonl` and `paced-152b.jsonl`, its
highest-ranked workhorse produced its first delta within 1.0 seconds on 43 of 51
calls. Across the workhorse's recorded under-five-second starts in
`experiments/.bench-one-note*/answers.jsonl`, 923 of 1,026 (90.0%) were inside one
second. A 1.5-second window would cover 49 of the 51 paced calls and 1,004 of 1,026
(97.9%) in the larger sample, but would also hold an already-answering reserve for
another half-second whenever the preferred lane is silent. The one-second default
chooses the shorter visible delay; callers wanting a stronger ranked preference
can increase it. The existing two-lane run retained only the winning lane's
timings, so it cannot replay exact replacement rates for candidate window values;
that direct measurement remains a post-implementation measurement rather than a
claim made from the old data.

### Selecting the authoritative answer

Choosing a provisional stream does not settle the race. Every viable lane keeps
running until one produces a complete valid answer or the caller's budget expires.

- If the provisional lane completes first, its answer is authoritative. The call
  ends normally and the remaining lanes are cancelled.
- If another lane completes first after provisional deltas have reached the caller,
  that complete answer is authoritative. The provisional stream terminates with a
  distinct exception carrying the complete replacement result.
- The caller handles that exception by discarding every provisional delta and
  replacing it with the supplied complete answer. No output from different models
  is ever spliced together.
- If a lane completes before any delta has been exposed, no replacement signal is
  necessary: its buffered answer can be delivered as the call's ordinary output.
- A real failure in a background lane is settled normally and does not interrupt a
  still-viable provisional lane.
- A failure or expiry in the provisional lane does not end the call while another
  lane can still complete within the shared budget. A later complete answer from
  that lane is delivered through the replacement path.
- If no lane completes inside the caller's budget, the call ends on the appropriate
  timeout or failure. Partial provisional output remains non-authoritative.

The replacement result must carry everything an ordinary completed routed result
does, including its model, call id, text, usage where available, operation context
and ability to receive the host's quality rating. After replacement, final call
identity and rating must name the model whose complete answer became authoritative,
not the model whose provisional deltas were discarded.

### What `fastest_of` means after this change

The final winner is still selected exclusively by speed: it is the first complete
valid answer. The bounded preference setting does not give the first pool model
extra time to win the completed race. It only reduces the probability that the
application begins rendering one model and later has to replace it with another.

In particular, if a reserve completes and the preferred model would also have
completed later but still within `wait`, the reserve remains the winner. Waiting
until the whole budget expires merely to discover whether the preferred answer
would eventually finish would defeat `fastest_of` and add latency after a complete
answer is already available.

## Consequences

- Streaming consumers opting into this behavior must implement whole-answer
  replacement. This is more work than consuming `AsyncIterator[str]` alone.
- An exception is intentionally loud for a consumer that has not implemented the
  new contract: it must not silently treat discarded provisional text as a
  successful final answer.
- Reserve output must be buffered up to a complete answer, so memory is spent in
  addition to the provider quota already implied by `fastest_of`.
- A model can be shown provisionally and still lose. "Produced output" therefore
  no longer implies "answered the routed call" under this mode.
- The existing no-splice boundary remains intact: replacement is whole-answer
  substitution performed by the host, never continuation of one model's text by
  another.

## Settled public contract

Add this keyword only to the routed async streaming surface:

```python
stream = broker.stream(
    prompt,
    fastest_of=2,
    stream_selection_window=1.0,
)
```

- Add `stream_selection_window: float = 1.0` to `AsyncBroker.stream()` and
  `AsyncLLMs.stream()`, and pass it through `AsyncLLMs._deltas()` to
  `Router.stream()`.
- Do not add it to `ask()`, `chat()`, the synchronous `Broker`/`LLMs` surfaces or
  either direct client. `ask()` and `chat()` already race complete `AsyncResult`
  objects and need no stream-selection rule; synchronous routed streaming does not
  exist.
- Validate the value before opening a provider request. Accept a finite non-negative
  `int` or `float`, excluding `bool`; reject negative, infinite, NaN and non-numeric
  values with `ValueError` naming the argument.
- Zero is valid. It removes the ranked preference from provisional selection, so
  the first useful delta becomes visible immediately, while the lanes still race
  to the first complete answer.
- The setting is harmless when fewer than two explicit `fastest_of` lanes actually
  run. It is still validated, but the call follows the existing single-lane path.
- No second switch is introduced. A streaming call with `fastest_of` unset or equal
  to one retains the current behavior; explicit `fastest_of > 1` activates the
  complete-answer streaming race and its replacement contract.

Add one public typed exception, `StreamReplacementError`, under
`LLMRequestError`. It carries:

- `replacement: AsyncResult`, the complete authoritative answer;
- `streamed_llm_name: str`, the model whose provisional deltas must be discarded.

The `AsyncResult` supplies the authoritative text, model, call id, usage, operation
and existing `record_quality()` behavior. Export the exception from top-level
`llmbroker`. A caller which catches `LLMRequestError` broadly must catch
`StreamReplacementError` first when it opts into streaming `fastest_of`; the
specific type makes the required whole-answer replacement impossible to confuse
with an ordinary failed request.

The exception is terminal for that iterator: after it is raised, no more deltas
belong to the stream. The minimal host-side contract is therefore:

```python
parts = []
try:
    async for delta in stream:
        parts.append(delta)
except StreamReplacementError as exc:
    text = exc.replacement.text
else:
    text = "".join(parts)
```

When replacement wins, update the original `StreamHandle` receipt to the
authoritative model before raising. After the exception, both
`exc.replacement.record_quality(score)` and `stream.record_quality(score)` rate the
same final call. During provisional output the handle may name the provisional
model, but remains unrateable until an authoritative answer has settled.

## Exact runtime semantics

Call the first acquired config `P` and every other explicit lane a reserve. The
selection window starts when the initial provider tasks have been scheduled, not
when the call began waiting for pool capacity. Its deadline is capped by the
remaining answer deadline where `wait` supplies one. A first delta timestamp equal
to the selection deadline is inside the window.

Before any output is exposed:

1. Consume every lane concurrently and retain its deltas.
2. If `P` produces a useful delta inside the window, select it provisionally and
   expose it immediately.
3. If a reserve produces first, keep consuming and buffering it. Select `P` if `P`
   begins inside the window; otherwise, at expiry select the reserve with the
   earliest first-delta timestamp.
4. If every lane is silent at expiry, select the next lane to produce a useful
   delta.
5. If `P` fails before producing a delta, end its preference immediately. There is
   no reason to hold already-buffered reserve output until the old deadline.
6. If any lane completes a valid answer before provisional output is exposed, that
   full completion wins immediately, including during the selection window. Replay
   its buffered provider deltas as the ordinary stream; no replacement exception is
   needed because the caller has nothing to discard.
7. Resolve genuinely simultaneous timestamps deterministically in acquisition
   order, which is already pool order.

After provisional output begins:

1. Continue consuming every viable lane, including the provisional one, without
   letting consumer backpressure stop the provider-side race.
2. Record the first complete valid answer as authoritative at provider completion,
   before journal-write latency can change which model was fastest.
3. If the provisional lane wins, cancel the remaining live lanes, yield every
   buffered delta from the winner in order, settle all attempts and end normally.
4. If another lane wins, stop yielding provisional deltas immediately, cancel the
   remaining live lanes, settle all attempts, update the handle receipt and raise
   `StreamReplacementError` with the winner's complete joined text.
5. If a hidden lane fails, retain its real verdict and refill the emptied lane from
   an untried candidate exactly as `_race()` does today, while candidates and the
   shared budget remain.
6. If the provisional lane fails or exhausts its budget, retain that exception but
   keep viable reserves running. A later complete answer replaces it. If no reserve
   can complete, surface the provisional lane's `StreamInterruptedError` or
   `LLMTimeoutError`, because that is the failure belonging to the partial text the
   caller saw.
7. Before any external delta, preserve the existing final-error precedence from
   `_route()`: an actionable client error beats generic expiry, otherwise exhausted
   capacity raises `NoLLMAvailableError` with its current reason and `retry_at`.

Useful means the same non-empty parsed text delta as today. A stream ending without
one remains an invalid empty answer and fails over. Completion means that the
provider stream ended normally after at least one useful delta; a partial buffer,
an error response or a malformed terminal chunk cannot win.

## Ownership in the current source

### Public plumbing

- `src/llmbroker/broker/broker.py`: add and forward the keyword on
  `AsyncBroker.stream()` only. Leave `AsyncBroker.ask()` and `.chat()` untouched.
- `src/llmbroker/broker/llms.py`: add the keyword to `AsyncLLMs.stream()` and the
  private `_deltas()` generator, forward it on both the initial route and the
  post-rebuild retry. Do not let `StreamReplacementError` enter the existing
  `NoLLMAvailableError` rebuild branch: it is a completed answer, not pool
  exhaustion.
- `src/llmbroker/exceptions.py`: define `StreamReplacementError`. Use a
  type-checking-only import for `AsyncResult` if needed to avoid coupling the
  exception and result modules at import time.
- `src/llmbroker/__init__.py`: import and add the exception to `__all__`; the
  existing exception-surface test requires every defined exception to be public.
- `src/llmbroker/broker/result.py`: keep `AsyncResult` as the replacement value and
  reuse `CallReceipt`; no second result hierarchy is needed. Adjust `StreamHandle`
  documentation so its identity is explicitly provisional until normal completion
  or replacement settles the call.

### Router split

`Router._route()` currently treats the first item yielded by a lane as the winner.
That is correct for `Router.chat()`, whose `_attempt()` yields exactly one complete
`AsyncResult`, and for the existing single/recovery streaming behavior. Do not
change that generic meaning.

In `src/llmbroker/broker/router.py`, make `Router.stream()` choose a new
stream-specific persistent race only when the caller explicitly requested
`fastest_of > 1`. Keep its current `_route(_stream_attempt, ...)` branch for an
ordinary stream and for recovery-owned parallelism without explicit racing. Thus:

- `ask()` and `chat()` retain `_route()`, `_race()` and first-complete behavior;
- a single stream retains direct provider backpressure and its present exception
  behavior;
- recovery-only protection retains its current first-delta commitment;
- explicit streaming `fastest_of` keeps all of its lanes alive to completion;
- `fastest_of` plus recovery still uses the current maximum width, not an added
  third lane.

Factor only candidate acquisition/refill/error precedence that the two drivers
genuinely share. Do not force persistent stream state back into `_Lane.open()`,
whose one-item contract is the reason atomic racing is simple.

### Persistent stream lane

Add a private stream-race lane state beside `_Lane`. It needs, at minimum:

- the config, `_Outcome` and `_stream_attempt` iterator;
- one background task which drains that iterator to termination;
- a list of parsed text deltas and an index marking how many have reached the
  caller, avoiding a second copy for the output queue;
- monotonic `first_delta_at` and `completed_at` timestamps;
- the lane-local `CallReceipt`, terminal exception/verdict and flags for provisional
  exposure and settlement.

The background task reports first delta, later delta, valid completion and failure
to one coordinator notification primitive. It must keep reading both the visible
and hidden provider responses so consumer speed cannot decide the race. Join the
winning lane's delta list only once, when constructing a replacement
`AsyncResult`; repeated string concatenation while streaming would make buffering
quadratic.

`_stream_attempt()` currently decides that any failure after `progress.started` is
unrecoverable and raises `StreamInterruptedError`. In a persistent race,
`progress.started` means only that the lane produced private data; it no longer
means all alternatives are gone. Refactor the attempt/driver boundary so the
attempt still applies and journals its real failure, while the persistent
coordinator decides whether to wait for another lane or expose the saved exception.
Keep the existing single-stream translation unchanged.

Capture `completed_at` when `_settle_stream()` has established that the response is
non-empty and normally terminated, before awaiting `_finish_ok()` and its store
write. The coordinator may publish the answer only after its `OK` row has settled,
but a slow store must not turn the second provider completion into the first.

### Budget and buffering

Keep the existing `queue_deadline`/`answer_deadline` construction and pass the same
absolute answer deadline to every initial and replacement lane. The selection
window neither extends nor divides it. `wait=0` keeps its existing special meaning
of no queueing deadline and uses the global provider ceiling for attempts.

The persistent driver drains providers in background tasks, so time the external
consumer spends between `anext()` calls is not provider waiting and cannot exhaust
`wait`. A provider may finish while the consumer is holding an earlier delta; its
completion timestamp still decides the race. Once the winning provider has
completed, replaying deltas already in memory is not placed under a new answer
timeout.

Do not introduce a separate buffer-size option in this change. Store deltas in a
list per active lane; memory is linear in the aggregate responses already admitted
by `fastest_of`. Closing the `StreamHandle`, cancellation of the consumer or any
unexpected coordinator exception must cancel and close every lane, finish any lane
already settling, release every acquired slot and leave no background task.

### Journaling and learning

Keep the existing `CallStatus` schema:

- the authoritative lane writes `OK`;
- a still-live lane cancelled because another completed writes `SUPERSEDED`, even
  if some of its provisional deltas reached the caller;
- a lane which reached a real provider/client/budget failure before cancellation
  keeps that real row and its existing cooldown or budget-bound effects;
- expiry of `stream_selection_window` writes nothing and teaches nothing.

Do not add a losing provisional call to the quality window. On normal completion
the authoritative `StreamHandle` is rateable; after replacement both the handle
and replacement `AsyncResult` name and rate the authoritative call. Preserve the
existing rule that a lane already settling cannot be overwritten as superseded,
and wait for every attempted lane's row and slot before the stream terminates
normally or raises its terminal replacement.

If the consumer explicitly closes an unfinished raced stream, preserve today's
host-abandonment semantics: the exposed lane is the call the host chose to stop,
so it writes `OK`, settles the handle to that lane and remains rateable. Cancel the
hidden lanes as `SUPERSEDED`, settle every row and slot, and do not infer a
completion-race winner from work the host explicitly abandoned.

## Tests bound to the current suite

### `tests/test_race.py`

Replace the current first-delta-wins expectations only for explicit streaming
`fastest_of` and add deterministic event-driven cases for:

1. both provider requests start before either is allowed to finish;
2. preferred first delta inside the default one-second window is exposed
   immediately;
3. an earlier reserve delta is buffered, then preferred begins inside the window;
4. buffered reserve output is released at window expiry when preferred is silent;
5. with both silent at expiry, the next first delta selects the provisional stream;
6. `stream_selection_window=0` removes the ranked provisional preference;
7. a complete reserve wins inside the window before anything was exposed and is
   replayed without replacement;
8. a hidden lane completes before the provisional lane and raises
   `StreamReplacementError` containing its whole answer, with no mixed deltas;
9. a provisional lane completes first and every reserve is cancelled as
   `SUPERSEDED`;
10. preferred failure ends its selection privilege immediately and the lane is
    refilled from an untried model;
11. a hidden real failure retains cooldown/journal evidence while another lane
    answers;
12. a provisional real failure is rescued by a later complete reserve;
13. more than two lanes use pool order only for the preferred lane and timestamps
    for reserves;
14. explicit racing plus recovery adds no lane beyond the requested/current maximum;
15. a one-model eligible pool and recovery-only parallelism retain their existing
    behavior;
16. closing or cancelling at each phase leaves every slot, task and journal row
    settled, preserving the current exposed-lane `OK`/rateable abandonment contract;
17. winner choice follows captured provider completion time rather than an
    intentionally delayed store write;
18. invalid window values open no provider request.

Update the signature test so `stream_selection_window` is present on
`AsyncBroker.stream` and `AsyncLLMs.stream`, absent from `ask`, `chat`, synchronous
and direct surfaces, and defaults to `1.0`.

### `tests/test_whole_answer_budget.py`

Add the cross-feature budget cases:

- a provisional lane which dribbles past the budget is replaced when a hidden lane
  completed inside it;
- no replacement exists when every lane is incomplete at the shared deadline;
- all lanes receive one deadline rather than a multiplied or per-lane budget;
- a slow external consumer changes neither completion order nor the journaled
  provider-time budget;
- buffered replay after provider completion is not timed out by consumer delay.

Leave the existing single-lane assertions intact: without explicit
`fastest_of > 1`, a post-delta expiry remains `LLMTimeoutError` and a post-delta
provider failure remains `StreamInterruptedError`.

### Public/result coverage

- Extend `tests/test_exceptions_surface.py` through its existing exhaustive export
  assertion and add focused checks for the new exception's inheritance and fields.
- Add caller/broker forwarding assertions where the current stream handle tests in
  `tests/test_router_stream.py`, `tests/test_callers.py` and `tests/test_broker.py`
  already cover receipt identity, rating and post-exhaustion retry.
- Verify that the handle names the provisional model while deltas arrive, changes
  to the authoritative model before replacement is caught, and rates only the
  authoritative call after settlement.

## Standing specification and documentation updates

Implementation must update the old first-delta commitment rather than leaving two
contradictory contracts:

- `specs/reference/invariants.md`, invariant 18: keep the no-splice rule, but admit
  that explicit streaming `fastest_of` output is provisional and may terminate in
  a typed whole-answer replacement.
- `specs/reference/decisions.md`, `parallelism-is-explicit-or-recovery-owned`:
  distinguish atomic first-complete, recovery-only first-delta and explicit
  streaming first-complete settlement; remove the block against replacement.
- `specs/reference/rules/call-path.md`, both parallelism and streaming sections:
  describe the selection window, persistent lanes, final receipt/rating and how
  failure after provisional output can still be rescued without splicing.
- `docs/src/en/usage.md` and `docs/src/ru/usage.md`: document the keyword beside
  `fastest_of`, its one-second default and quota/buffering/replacement consequences.
- `docs/src/en/async.md` and `docs/src/ru/async.md`: show the required
  `StreamReplacementError` catch and whole-text replacement, while retaining the
  simpler example for a non-raced stream.
- Update the pooled/direct exception lists in both documentation languages where
  `StreamInterruptedError` is currently enumerated.

## Verification and downstream measurement

Run the focused race, stream, budget, caller, broker and exception suites, then the
project's normal full preflight. No store migration should appear: the plan reuses
`OK`, `ERROR` and `SUPERSEDED` rows.

After the library behavior passes deterministically, switch echo-words' existing
source-level streaming arm back on, catch the replacement exception by replacing
the accumulated article, and rerun its fixed production-prompt smoke tier with
`fastest_of=2`, `wait=25` and the default one-second window. Record completion
rate, provisional model, authoritative model, replacement count, visible first
delta, whole-answer latency, semantic review and provider usage. This fresh run is
the measurement the old winner-only data cannot reconstruct.

## Out of scope

- Any preference for the first pool model when choosing the final complete answer;
  adding that to `ask()` as well would be a separate change to `fastest_of` itself.
- Changes to candidate ordering, cooldown, quality demotion or budget-aware
  ordering.
- An LLM judge or other semantic scoring on the routing path.
- A separate buffer-limit, completion-grace or deadline-reserve option.
- Synchronous or direct streaming.
- A promise that the fastest complete answer satisfies the host's semantic quality
  bar; the host still rates the authoritative call.

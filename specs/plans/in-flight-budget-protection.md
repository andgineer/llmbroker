# Plan — protect one in-flight call from a silent first choice

**Status: implemented.** This document records the behavior that was agreed and
then built. Where the discussion left a point open, the decision taken while building
it is in *Settled while building* below, and the reasoning behind the review round that
followed is in the handover.

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

The default comes from echo-words' interactive paced measurements of the pool's
highest-ranked workhorse. It produced its first delta within 1.0 seconds on 43 of
51 calls, within 1.25 seconds on 46, within 1.5 seconds on 49 and within 2.0
seconds on 50; the larger one-note sample shows the same knee, with 90.0% of its
1,026 recorded under-five-second starts inside one second and 97.9% inside 1.5.

A second half-second would therefore avoid six more likely replacements — and would
hold an already-answering reserve back by that same half-second on every call where
the preferred lane is silent. **One second is the default because the shorter visible
delay is the one the reader feels**; a caller who wants the ranked preference to hold
longer raises the number, and that is what the setting is for. The existing two-lane
run retained only the winning lane's timings, so it cannot replay exact replacement
rates for candidate window values — that measurement is still outstanding, see
*Still open*.

### Selecting the authoritative answer

Choosing a provisional stream does not settle the race. Every viable lane keeps
running until one produces a complete valid answer or the caller's budget expires.

- If the provisional lane completes first, its answer is authoritative and the call
  ends normally.
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

Whichever lane wins, **every other lane comes off its provider at the instant that
answer exists**, not when the consumer next asks for a delta. A reader that pauses
between deltas may not be charged for provider work the race has already decided
against, and a reserve that has not opened its request yet never opens it.

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
- A raced stream leaves exactly one answered call in the journal. Because the first
  completion takes every sibling off its provider in that same instant, no second lane
  reaches a completion of its own. A race of ordinary completions is unchanged and can
  still leave two, which is why a race is rated through what it hands back.
- A lane taken off before it reached its provider is journaled not at all: it made no
  call, so there is no row to write and the pool learns nothing from it.

## Settled while building

- **The setting alone switches the behavior on.** There is no second explicit flag: a
  stream with more than one `fastest_of` lane races complete answers, and one without
  is the ordinary stream it always was.
- **Zero is legal and means no ranked preference** — the first text to arrive is what is
  shown. Anything negative, non-finite or non-numeric is refused before a single
  provider request opens, as `fastest_of` already is. The setting has no effect on a
  call with one lane, and is not offered where it could not mean anything.
- **A buffered reserve that completes inside the preferred lane's interval settles the
  race at once.** The general first-complete rule wins: the interval only ever decided
  what is *shown*, and there is nothing left to show once an answer exists.
- **A failing preferred lane gives up its interval immediately** rather than holding it
  out to the end. A lane that will never begin has nothing left to hold back.
- **The replacement is a full routed result** — text, model, call id, usage, operation
  context and the ability to take the host's rating — and the handle names the winner
  before the exception is raised.
- **A replaced provisional lane is journaled as superseded**, the neutral outcome that
  already existed. No new outcome was added: nothing about losing a race is availability,
  budget or quality evidence, and inventing a row type would have invited it to become
  one.
- **Recovery-owned parallelism adds no lane.** Whichever of the two is wider is the
  width, exactly as it was before this change.
- **The budget is provider time and is paused for every lane alike** while the consumer
  holds a delta, which is the rule the whole-answer budget already had.
- **When no lane can complete, the failure raised is the one belonging to the text the
  caller saw**, held until the last reserve is gone rather than raised in front of a
  still-viable one.

## Still open

- The controlled measurement against alternative semantics has not been run. At minimum
  it should compare completion rate inside the host budget, authoritative-model
  distribution, host-rated quality, visible first-delta latency, replacement frequency,
  whole-answer latency and duplicate provider usage — and, with per-lane timings kept
  this time, the replacement rate at candidate window values.

## Out of scope

- Any preference for the first pool model when choosing the final complete answer.
- Changes to cooldown, quality demotion, pool priority or budget-aware ordering.
- A promise that the fastest complete reserve answer satisfies the host's semantic
  quality bar; host ratings and pool ordering remain the available quality signals.

## Handover

What follows is the review history, which the body above deliberately does not carry.

### The two findings from the first review

**Retirement no longer waits for the reader.** The losing lanes came off their providers
only when the consumer next pulled, so a slow reader paid for provider work the race had
already decided against. They now come off at the instant a provider completes. In the
common case a sibling is taken off before it has opened a request at all.

**The duplicate-answered-row warning named the wrong surface.** It is a race of
*completions* (`chat`/`ask` with `fastest_of`) that can leave two answered rows, not a
raced stream. Corrected in `selection.md`, `call-path.md` and the EN/RU docs.

### What the first fix pulled in behind it

The first two entries are in *Consequences* above, and are flagged here because they
**narrow** a contract the first review had just read the other way: a raced stream now
leaves exactly one answered call, and a lane taken off before it reached its provider
is journaled not at all.

The third is a defect rather than a consequence. **A pool slot was being leaked**
(invariant 19): a lane cancelled before its attempt began ran none of its own code, so
nothing handed back the slot the driver had acquired for it. The driver now does. The
hole pre-dated this work — an early consumer abandon could reach it — but retiring at
the instant of completion makes it the common path.

Left where it is: the completion race still settles through a wait on its lanes, so two
of them may both finish and both keep an answered row. That duplicate is a genuine tie,
not a delay, and removing it would need the same completion callback on the
non-streaming attempt.

### Tests

Four race tests changed because they had depended on a loser still being alive after
the winner completed; three now hold the winner back until every lane is on its
provider, and one became a unit test of the race's own winner selection, whose scenario
is no longer constructible. Two were added: retirement while the reader is paused and
the winner's row still unwritten, and the slot going back for a lane cancelled before
its attempt began.

### Gate

`invoke pre` clean (ruff, ruff-format, docstring cap, pyrefly: 0 errors).
`python -m pytest`: 1562 passed, 0 failed, 0 skipped.

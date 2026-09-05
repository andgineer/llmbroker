# The call path

What happens between `ask`/`chat`/`stream` and a result, once a candidate model
has been picked. Which model that is belongs to
[`selection.md`](selection.md). The cross-cutting rules this file elaborates are
in [`../invariants.md`](../invariants.md).

There is one pool per process and every caller routes over it, so the per-model
concurrency cap is the installation's: the slot counter that enforces it is the
pool's, and a counter per caller would not be a cap at all
([`decisions.md`](../decisions.md#the-broker-is-the-installation-a-caller-is-a-scope)).
What differs per caller is which models it can pay for — a model whose key that
caller does not hold is not a candidate for it, and the exhaustion reason a caller
sees is computed against its own keys.

## The error contract

When no model can answer, the broker raises `NoLLMAvailableError` carrying a
machine-readable `reason` (empty pool, no resolved key, all disabled, every
candidate excluded for this call, or a genuine timeout) and, when the pool is
only temporarily exhausted, `retry_at` — the earliest time a model is expected
back. The caller gets a result or an exception, never silence.

Lifecycle failures form their own tree rooted at `RuntimeError`, separate from
the request-error tree. The two are different axes, and rooting lifecycle errors
at `RuntimeError` keeps hosts that already catch it around provisioning working
unchanged. A fatal condition carries as attributes the facts a host would
otherwise parse out of the message. Every exception a host may catch is on the
top-level package surface, both trees and both bases, so nothing a caller has to
handle is reached by a submodule path.

## Classifying a failure

**Below the status line, everything is the provider's fault.** A transport
failure of any kind — connect, read, write, protocol, proxy, timeout, or a plain
OS socket error — cools the model down, is journaled, and fails over to the
next. So does an HTTP 200 whose body is not an OpenAI-compatible chat
completion: undecodable JSON, or a shape with no assistant message, surfacing as
`InvalidProviderResponseError` carrying the model name and a truncated body
snippet. An endpoint answering 200 with garbage misbehaves no less than one
answering 503, so a caller never receives a raw transport or parsing error from
a pool call while another model could still answer.

**A 200 that parses and says nothing lands there too.** A completion with no text
and no tool calls is the same type on the same surface, because it is the same
fact — a candidate produced nothing usable while others were untried
([`decisions.md`](../decisions.md#an-empty-answer-is-a-failure)). One reading of
an unusable 200 serves both call paths, so the direct client raises on it where
the pool fails over. A reply carrying tool calls and no prose is an answer.

An unexpected exception is a bug and does reach the caller; a cancelled call
propagates untouched.

**A client request error fails over without cooling anything.** It excludes the
offending model for the rest of that call only — a later request may use it
immediately. When every candidate rejects the request this way, the last
provider error is raised rather than a generic "no LLM available": the fault
is in the request and only the provider's own error is actionable. It also
outranks a `wait` budget that expires later in the same call — an error the
caller can act on beats "the clock ran out".

**What surfaces is llmbroker's own provider-error type, carrying the status and a
body snippet — the same type a direct call raises on the same answer.** One
mapping serves both call paths, so a host catches one hierarchy rather than also
catching the transport library's exceptions, which invariant 20 does not admit as
a contract.

**Malformed means malformed in the answer.** A reported token count that no
64-bit integer column can hold is discarded and the answer returned. The reply
is what the caller asked for, so failing the call and cooling the model over an
unusable accounting field would trade a good answer for none — and the discard
is not cosmetic: a count the journal cannot store loses the whole row, and with
it the call the pool needs to learn from.

## What a routed request may carry

**A routed parameter is admitted one at a time, by name, and admitting one means
measuring what the curated pool does with it**
([`../decisions.md`](../decisions.md#the-pool-takes-named-parameters-one-at-a-time)) —
the pool's members are interchangeable only because each is sent the same
request, and the arbitrary mapping a model reached by name accepts
([`direct-by-name.md`](direct-by-name.md)) is therefore not offered here.

**A constrained request is one request like any other.** Every candidate is asked
the same thing, failover included, and what comes back is judged exactly as any
other answer: a reply that ignores the constraint is a successful call, not a
failure to fail over from. Nothing here reads the caller's schema, so there is
nothing else it could be — and the host's own validation, fed back as a quality
rating, is the signal that already orders the pool
([`selection.md`](selection.md)).

## `wait` — the caller's budget for the routing path

It bounds both halves of a call: how long the broker may queue for a slot, and
how long the picked model may take to answer, so a provider that accepts the
connection and then hangs cannot outlive the budget.

It does **not** bound the broker's own bookkeeping between attempts. Each failed
attempt is journaled before the next starts, so a call failing over across
several models overruns `wait` by the store's write latency. That ordering is
deliberate: an attempt is settled whole — its slot handed back, its cooldown
applied, its row written — before the next candidate is picked, so a failover
never leaves a half-recorded attempt behind it.

- **`None`** (the default) waits as long as at least one model can still come
  back by itself — a cooldown expiring, a capped slot releasing — and raises
  immediately when none ever will (an empty pool, every model keyless, every
  model disabled, every candidate excluded for this call). The in-flight attempt
  then falls back to a single global HTTP ceiling.
- **`0`** means "do not queue", not "answer instantly": every currently-free
  model is tried, no cooldown and no busy slot is waited on, and each attempt
  runs under the global ceiling.
- **Negative** means the budget is already spent: both slot acquisition and the
  attempt short-circuit, and the call raises without opening a request. It needs
  no validation of its own — `0` is the boundary that carries the special
  meaning.

**For a stream, `wait` bounds the whole answer, measured in provider time.** The
budget covers acquiring a slot, reaching the first delta and everything after it,
so an answer is inside it or not however it was chunked — a model that opens at
once and then takes a minute is no more inside a ten-second budget than one that
says nothing for a minute. What the budget never counts is the consumer: the clock
is disarmed whenever a delta is handed over and pushed on by however long the
reader held it, so processing slowly can no more spend the budget than it can be
blamed for the model. This is the same meaning the budget has for a completion,
which is the point: a caller asks for an answer within a time, not for a first
token within a time.

**What a spent budget says about the model depends on what the model had produced
when it ran out**
([`decisions.md`](../decisions.md#silence-cools-and-teaches-ordering)). Nothing at
all is silence: to that caller the endpoint is indistinguishable from a dead one,
so it is cooled and its streak advances like any other failed attempt. An answer
already arriving is not: the model answered and the caller merely stopped waiting
for the rest, so it is neither cooled nor counted, and cooling it would withdraw a
working model over one caller's setting. A budget already gone before the provider
was opened teaches neither — the model never got its chance.

Every one of them journals a plain `ERROR` row: an expiry is journaled for
visibility, not classified, so there is no status of its own to read it back by.
The two that happened before any output raise `NoLLMAvailableError` with a timeout
reason, and `retry_at` follows the same rule there as everywhere else: it names a
moment only when nothing can serve that caller right now. What ran out is the
caller's clock and not the pool, so while a sibling is free there is no better
moment than this one — but where the silence cooled the last candidate standing,
when it comes back is known and is reported. An expiry mid-answer ends the stream on its own
timeout instead, under *Streaming* below. The global ceiling firing is a different
thing again: no budget was set, so a model that outlives it is genuinely too slow
and cools like a 5xx.

An expiry that reached the provider also teaches ordering; how, and under what
limits, is in [`selection.md`](selection.md).

## More than one model at a time

A routed call may run over several distinct models at once, for two unrelated
reasons, and both are options on the routed surfaces only — a model reached by
name is not a pool ([`direct-by-name.md`](direct-by-name.md)).

**The caller may ask for the fastest of N models.** It reserves up to that many
currently-available entries, in the ordinary order of acquisition
([`selection.md`](selection.md)); a smaller eligible pool simply runs fewer. It
never multiplies the budget: every lane runs against the caller's one deadline,
and invariant 7 still holds — there is no per-model timeout underneath it.

**Separately, the pool covers its own recovery work.** The first attempt on a
model whose cooldown has passed runs beside an ordinary candidate by default, so
uncertainty the pool itself created is not paid for out of the caller's latency.
That protection is opportunistic — it never waits for a second candidate to come
free — and a caller may switch it off where provider quota matters more than
latency ([`decisions.md`](../decisions.md#parallelism-is-explicit-or-recovery-owned)).
Both together add no third lane: whichever is wider is the width, and the
caller does not need to coordinate the two options.

**What commits the call is the first thing a model produces.** For a completion
that is the first complete valid answer; for a stream it is the first delta, which
reaches the consumer immediately and fixes what answered — a lane still opening is
never waited for, and after that point no other model may replace the one
answering (invariant 18).

**Every other lane is then cancelled and journaled as superseded, which teaches
nothing.** It never cools a model, never advances or resets a failure streak,
never raises or clears a budget bound, and never enters a quality window: losing a
race proves only that a sibling was quicker by that instant. It is still an
observation a host can read, naming the model, the operation, the trace and the
elapsed time, and carrying usage where the provider reported any before the
cancellation. A lane that had already reached a real failure keeps that failure and
everything the pool learned from it — supersession is not applied after the fact.

**A stream's settlement never stands between a delta and the consumer.** A lane still
waiting on its provider is cancelled at once; one that has already left it — by a
classified failure, by an answer, or by an unexpected exception — is settling itself and
is waited for instead, because a verdict applied in memory whose row never lands is
evidence silently lost (invariant 8), and a release cut in half loses the slot
(invariant 19). Those rows and slots are handed back beside the deltas the consumer is
already reading. A completion has no such *beside*: it reaches the caller when the call
ends, and a call that raced ends only once every attempt it made is journaled.

**Failures inside a race are disposed exactly as they are alone**, through the
classification above, and the lane they empty is refilled from a model this call
has not tried, while both the budget and the candidates last.

Racing spends provider quota on answers nobody reads, which is why an ordinary
healthy call stays on one lane.

## Streaming

The routed pool streams as well as it answers: deltas arrive as the provider
produces them, over the same routing, failover and journaling as a pooled call.
It is async-only, like the direct client's streaming.

**A streamed call names what answered it**, like every routed call: what it hands
back carries the model and the call id its deltas came from, plus the token counts
once the attempt is over, so a host can attribute or rate the answer without a
journal read — which a non-queryable store would not allow at all
([`decisions.md`](../decisions.md#identity-rides-the-object-a-call-returns)).

The naming is unset until the call settles on a model, and fixed from then on.
That is the first delta; a stream the consumer walked away from before one arrived
settles when it is closed, which is why the `OK` row that ends it is named too
rather than left anonymous. Earlier it must stay unset: failover may still move,
so any value before that point would name a model that did not answer.

**Rating through what the call handed back is refused until the call is
journaled**, and a stream reaches that point only when its answer ends — closing
an abandoned stream is what ends it. Offering it sooner would let a rating be
appended before the row it names, which invariant 1 forbids.

**A budget exhausted mid-answer is a miss, not a failure.** The model answered, so
it is not cooled and its streak does not advance; what it did not do is finish,
which is recorded as a budget it did not finish within — the same evidence a
pre-delta expiry leaves, and it reaches ordering the same way
([`selection.md`](selection.md)). The call ends by raising the timeout error,
distinct from a mid-stream death (invariant 20); the deltas already yielded stand,
and no failover follows (invariant 18).

Every failure before the first delta — 429, 5xx, transport, malformed response,
and an attempt that ended without ever producing a delta — cools the model and
moves to the next candidate through the same classification above; there is no
second failure surface for streams. Nothing reached the caller in any of those, so
failover is still open and invariant 18 is untouched: it binds only *past* the
first delta. After the
first delta a mid-stream death cools the model (it misbehaved no less than one
failing earlier) and raises, carrying the model name and the underlying cause;
the deltas already yielded stand.

Each attempt journals one row, as `chat` does. **A consumer that stops pulling
ends a successful attempt** — the model answered and did nothing wrong, so the
row is `OK`. Abandoning an iterator must never cost a model a *failure*.

**The slot goes back when the iterator is closed, and closing it is the
consumer's move.** An async generator has no other signal: the broker cannot
tell "paused between deltas" from "never coming back", and the provider
connection is still open either way, so holding the slot until close is correct
rather than conservative. Python closes the iterator for the ordinary shapes —
`break`, an exception through the loop, a cancelled task — because the last
reference drops there. A consumer that keeps the iterator in a variable and
walks away holds the slot until the event loop finalizes it, so a long-lived
host that abandons streams that way must close them itself. This is the standard
async-generator ownership contract, not a broker rule.

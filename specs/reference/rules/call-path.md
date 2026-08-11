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
otherwise parse out of the message. The top-level package exports the lifecycle
base and the benign, application-handled member of the tree; an
operator-actionable deployment failure stays reachable through
`llmbroker.exceptions` without being promoted onto the package surface, which is
reserved for what an application catches in normal operation.

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

An unexpected exception is a bug and does reach the caller; a cancelled call
propagates untouched.

**A client request error fails over without cooling anything.** It excludes the
offending model for the rest of that call only — a later request may use it
immediately. When every candidate rejects the request this way, the last
provider error is re-raised rather than a generic "no LLM available": the fault
is in the request and only the provider's own error is actionable. It also
outranks a `wait` budget that expires later in the same call — an error the
caller can act on beats "the clock ran out".

**Malformed means malformed in the answer.** A reported token count that no
64-bit integer column can hold is discarded and the answer returned. The reply
is what the caller asked for, so failing the call and cooling the model over an
unusable accounting field would trade a good answer for none — and the discard
is not cosmetic: a count the journal cannot store loses the whole row, and with
it the call the pool needs to learn from.

## `wait` — the caller's budget for the routing path

It bounds both halves of a call: how long the broker may queue for a slot, and
how long the picked model may take to answer, so a provider that accepts the
connection and then hangs cannot outlive the budget.

It does **not** bound the broker's own bookkeeping between attempts. Each failed
attempt is journaled before the next starts, so a call failing over across
several models overruns `wait` by the store's write latency. That write stays on
the call path deliberately: the journal is the shared state a sibling node reads
a cooldown from, and a caller released before the row lands would let the next
one repeat the failure.

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

**For a stream, `wait` bounds the wait for the first delta.** A stream has no
single "the attempt finished" moment, so the budget covers the one stretch still
the broker's to rescue: acquiring a slot and reaching the first delta. Past it
the pace is the consumer's as much as the model's — a caller that processes
deltas slowly suspends the stream between them — so blaming the model for the
wall clock there would be blaming it for its reader. Every read after the first
delta still runs under the global ceiling, so an endpoint that goes quiet
mid-answer dies rather than hanging.

**A spent budget is never a model's fault.** The model is not cooled, its
failure streak does not advance, the call raises
`NoLLMAvailableError` with a timeout reason, and the journal row records no
cooldown. Only the global ceiling firing means the model is genuinely
too slow, and that cools it like a 5xx. Without the distinction a tight `wait`
would teach the broker that healthy models are failing. The row is a plain
`ERROR` one — an expiry is journaled for visibility, not classified, so there is
no status of its own to read it back by. Nothing is cooling either, so the
raised error carries no `retry_at`: there is no moment at which retrying would
be better than now.

An expiry still teaches ordering; how, and under what limits, is in
[`selection.md`](selection.md).

## Streaming

The routed pool streams as well as it answers: deltas arrive as the provider
produces them, over the same routing, failover and journaling as a pooled call.
It is async-only, like the direct client's streaming.

Every failure before the first delta — 429, 5xx, transport, malformed response —
cools the model and moves to the next candidate through the same
classification above; there is no second failure surface for streams. After the
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

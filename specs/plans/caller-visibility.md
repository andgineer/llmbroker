# Plan — what a caller can see of a call it made

**Status: functional only.** This states what a host can do after each change and
what the evidence for it is. Nothing here is bound to a module, a signature or a
test yet; that binding is the next step, and until it is done no `src/` line is
written.

## Goal

Four values the library already holds and does not hand back. A host that streams
cannot read what its call spent, cannot ask the journal what a named model did,
cannot tell from a failure whether the reader has already seen part of an answer,
and cannot read the latency the library measured on every call — including the
bound it teaches itself from budget expiries. Each is a value stopping one step
short of the caller.

## Why this is not the failure mode the queue warns against

[`README.md`](README.md) names the check: a mechanism sized for a problem this
pool does not have. Every item here fails that check in the opposite direction —
the mechanism exists and stops short of the caller:

- The streaming direct client already asks the provider for token usage, already
  parses it out of the stream, and then discards it.
- The journal already records latency per call, and the aggregate surface exposes
  counts by status only.
- Budget expiry is already learned into a per-model latency bound, and nothing
  reports the bound.

None of this adds a subsystem.

**What one host wrote instead**, which is what leaving them unbuilt costs: a
per-day counter of paid calls with its own UTC roll-over, a per-language record of
the last call and how it ended for a status screen, and per-call latency timing
inside its own harness. None of that is domain logic, and the next host writes it
again.

## The evidence base

The numbers below come from one host — a streaming, interactive vocabulary tool on
the free pool with an opt-in paid step — over its benchmark tiers: a 120-request
burst run at two caller budgets across three source languages; a 179-fixture
comparison of the pool against a paid model under an identical prompt; six
157-fixture prompt arms; a 218-call paid tier. One host's numbers on one workload,
not a survey.

Items keep the numbers they had in the plan this was split from, so the evidence
and the cross-references still line up.

---

## 2. A streaming direct call reports what it spent

**What a caller can do after this:** read token usage from a direct call it
streamed, the way it already can from one it awaited whole.

**Why.** The streaming path explicitly asks the provider to include usage, parses
it out of every chunk, and drops it on the floor; the non-streaming path returns
it. An interactive application streams — that is the whole reason it is on this
library — so the one call shape that reports cost is the one such a host never
makes. The host's own record of what a paid tier costs per month is therefore an
estimate reconstructed by hand, not a measurement, on data the library held and
threw away.

**Boundary.** Reporting a number the provider sent. No accounting, no aggregation,
no cap — see item 3 for where the numbers accumulate, and
[`decisions.md#no-rate-limits`](../reference/decisions.md) for what stays out.

## 3. A direct call leaves a trace in the journal

**What a caller can do after this:** ask the journal what a named model did — how
long it took, what it spent, under which trace id — and answer questions about the
paid path with the same query that answers them about the pool.

**Why.** A direct call today is invisible: no row, no call id, no trace id, no
latency. What the host built beside the library to compensate is small but it is
all replacement: a per-day counter of paid calls with its own UTC roll-over, a
per-language record of the last call and how it ended for a status endpoint, and
per-call latency timing inside its benchmark harness. None of that is domain logic;
it is a journal, re-implemented because the library's own journal stops at the pool
boundary.

**Boundary, and the two decisions this must not cross.** This is **not** a rate
limit or a cap — [`decisions.md#no-rate-limits`](../reference/decisions.md) stands,
and a host that wants a daily ceiling computes it from the journal itself. It is
also not logging: [`decisions.md#store-is-not-logging`](../reference/decisions.md)
stands, and nothing here emits text for a human.

The standalone client — constructed by a caller with a base url and a key, holding
no store — stays exactly as it is: no pool, no failover, no journal. A client the
broker hands out already holds the installation's store, and that is the one that
records. The distinction is the feature, not an exception to it.

**What must not follow from it:** failover, retries, quality learning or pool
membership for a named model. It is journaled because the host asked for it, not
routed because the broker chose it.

## 4. A failed request says whether anything reached the caller

**What a caller can do after this:** decide, from the failure alone, whether the
user has already seen part of an answer.

**Why.** For a streaming host that is the difference between two different products,
and it is measured. Same 120 requests, three languages, only the caller budget
moved:

| budget | answered | what a miss was |
|---|---:|---|
| 25 s | 64 of 120 | **56 of 56** ended before any delta: nothing shown, a clean fallback |
| 45 s | 110 of 120 | 8 of 10 died with text already delivered: half an answer on the page |

The host sets the tight budget for exactly this reason, and its fallback to a paid
model, its decision to blank what is on screen, and its rating of the pool call all
branch on that one bit. Today it is spread over three failure shapes, and a host
must know all three to read it: a budget that expired with no model answering, a
budget that expired while an answer was already arriving, and a stream that died
after deltas. Two of those mean output has reached the reader and one does not, and
nothing states it — the host derives it from which shape it caught.

**Settled shape: the fact rides the handle, not the failure.** A caller catches
whatever it catches, then asks the object it already holds what that call produced.

- It is where this library already puts facts about a call
  ([`decisions.md#identity-rides-the-object-a-call-returns`](../reference/decisions.md)):
  the handle names the model that answered, the call id and the usage, and how much
  of the answer left the library is the same kind of fact.
- Coverage is complete rather than partial. Only a stream can deliver half an
  answer, and a stream always has a handle; for an awaited call the answer is
  trivially "nothing", so nothing is needed there.
- It answers for failure shapes nobody enumerated in advance, including ones a
  later release adds.
- The exception hierarchy is untouched. No new class —
  [`decisions.md#one-error-with-a-reason`](../reference/decisions.md) is right that
  these causes differ in data rather than in handling — and no field on a base class
  that half its subtypes could never mean anything by.

The price is that a host must keep the handle in scope in its failure branch. The
one host that needs this already does.

**Second, smaller asymmetry, same item.** A stream that died names the model that
died; a request that never found one names nothing. A host reporting "the last call
for this language failed" therefore cannot say what was tried. Whether the attempt
chain belongs on the error or only in the journal is for the concretization step to
settle — the journal may well be the honest place.

## 5. Latency is derivable, so derive it

**What a caller can do after this:** read per-model, per-operation answer latency
from the broker, and see the latency bound the broker has learned for itself.

**Why.** Two halves, one read.

The first: the journal records latency on every call and the aggregate surface
exposes counts by status only. Every latency table the host has ever published —
which model is fast, which fallback breaks the interactive budget, what a paid tier
costs in seconds — was computed outside the library from its own harness's JSON,
over calls the library had already measured.

The second matters more. The pool's ordering problem is real and measured: the
curated weights put the two *slowest* free models directly behind the primary, so
the first fallback for an interactive caller answers 20–60 times slower — 36–57 s
against 1.6 s, worst case 101 s. The library's answer to this is already
implemented: an expired budget teaches a per-model latency lower bound that reorders
the pool for equally tight budgets
([`decisions.md#budget-expiry-teaches-ordering`](../reference/decisions.md)). It is
the right answer. **But nothing reports it** — a snapshot shows a model's cooldown
and its demoted operations, and not the bound — so no host can tell whether it ever
fired, and the one host that would have measured it could not.

The contrast is the argument. Of the two things this library learns from a tight
caller budget, one is visible and one is not, and the visible one is what produced
the standing case for a streak decay: the cooldown ladder a host could read off its
snapshots is the whole evidence there is for it. The latency bound learned from
the very same budget expiries left no trace a host could read, so nothing analogous
could be found in it. Whatever this plan does elsewhere, that asymmetry is worth
closing on its own.

**Boundary.** Derived from the same tail read as the quality windows, in the spirit
of [`decisions.md#aggregates-derived-not-accumulated`](../reference/decisions.md).
No new state, no accumulator, no second subsystem beside the journal.

## What stays out of this plan entirely

- Any promise about the *content* of an answer.
- Caps, budgets, quotas or spend limits of any kind —
  [`decisions.md#no-rate-limits`](../reference/decisions.md) stands, and a host
  wanting a daily ceiling computes it from the journal itself.
- Logging: [`decisions.md#store-is-not-logging`](../reference/decisions.md)
  stands, and nothing here emits text for a human.
- Failover, retries, quality learning or pool membership for a model reached by
  name. It is journaled because the host asked for it, not routed because the
  broker chose it.

## What the concretization step must add

For each item: the exact surface a caller touches; which existing module owns it;
whether the change is additive to a public type or a new one; and the tests that
pin the behavior, specifically the negative ones — a discarded value stays
discarded when the provider sends none, a standalone client still journals
nothing. Also whether item 4's attempt chain belongs on the error or only in the
journal, and whether these ship as one release or three.

## What moves into the specs

Item 3 draws a boundary a future reader will otherwise re-propose as a gap: what a
journaled direct call is *not*. That is a `decisions.md` entry naming the
alternative it rejects, written in the batch that implements it. Item 5's ordering
evidence, if it is re-measured here, belongs where the free-tier knowledge already
lives.

## Gate

`invoke pre` and the full suite green, per `CLAUDE.md`. Nothing here needs a
provider key: every item is a value crossing the library's own boundary, and a
fake provider pins all of them.

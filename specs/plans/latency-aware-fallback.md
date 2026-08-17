# Draft: what an interactive caller still needs

**Status: draft, deliberately not queued.** There is no row for it in
[`README.md`](README.md), so nothing picks it up. It says what to implement, why,
and roughly how; the work order, the tests and the spec moves belong to detailed
planning. It carries **four plans and one out-of-band edit**, and names which of
them must ship together.

**Plans 1 and 2 below are superseded and must not be implemented from this file.**
They are detailed in [`observed-latency-ordering.md`](observed-latency-ordering.md)
and [`stream-stall-timeout.md`](stream-stall-timeout.md), which are the queued
plans and the only ones to work from: detailed planning settled the statistic, the
lifetime of the evidence and an amendment to invariant 8, and dissolved this file's
"giving up on a slow model erases the evidence" into the two-number design. What is
still live here is **Plan 3 and Plan 4**. The out-of-band catalog edit has shipped.

## Where the evidence comes from

A downstream application — a vocabulary tool, one user, a couple of dozen words a
day, answers streamed into a browser — drove the free pool and the paid direct
client over ~40 items per source language across three languages, in two load
profiles: a burst of four concurrent requests, and a paced single-user session.
Provider-level observations from that run are in
[`../reference/freetier-providers.md`](../reference/freetier-providers.md#what-one-real-workload-met-measured-2026-08-17)
and are not restated here.

The pool is excellent at what it is for: paced, one user got 20 answers of 20
from the highest-weighted model, first delta 0.8 s, whole answer under 2 s, no
failover at all. Everything below is about the edges around that.

---

# Plan 1 — latency enters the fallback order

**Superseded — implement [`observed-latency-ordering.md`](observed-latency-ordering.md),
not this section.** What follows is the reasoning that produced it, kept for the record.

**Ships with Plan 2.** Without it the motivating case is not closed; see the cost
at the end of this plan.

## The problem

Under the burst profile the pool spilled to its second and third choices, and
those are the slowest endpoints it has:

| answering model | curated weight | median whole answer |
|---|---|---|
| `google-gemini-3.5-flash-lite` | 0.75 | 1.6-1.7 s |
| `openrouter-nemotron-3-ultra` | 0.72 | 36-57 s (worst 101 s) |
| `openrouter-laguna-s-2.1` | 0.70 | 43 s |
| `groq-gpt-oss-120b` | 0.55 | 2.8-3.5 s |
| `zai-glm-4.7-flash` | 0.55 | 34 s, of which 31 s before the first token |

The two slowest carry the highest weights after the primary, so the *first*
fallback is the worst choice an interactive caller could be handed, and the
fastest alternative sorts last. The last row is the sharpest case: a reasoning
model that emits nothing for 31 seconds and then answers well. Its weight says it
is worth routing to, and by the only thing weight speaks about — the quality of
what comes back — it is right. Nothing in the pool can say "never hand a
streaming caller this one".

## Why the existing bound cannot carry it, and why ratings cannot either

**The miss bound erases itself against exactly this case.** A model's own success
retires every miss older than it, on the live path through `_finish_ok` and again
on a rebuild through `budget_bounds_from_calls`. That is deliberate and correct
for what the bound is: a record of *not answering in time*. But a slow model
answers successfully every time — that is the entire complaint against it — so
evidence derived from successful rows would be wiped by the rows it was derived
from. Widening the existing bound is therefore not available; this needs a second
number beside it.

**Host ratings cannot do the job, and no threshold makes them.** A demotion
verdict needs `quality_min_count` ratings in one `(model, operation)` bucket, and
the curated weight keeps its majority until a window of comparable size fills.
But a fallback is only picked when the primary is unavailable, which at the paced
single-user load this library names as its own scale never happened once in 20
requests. A model never picked is never rated, its window stays empty,
`is_demoted` is permanently False and its priority is permanently the curated
weight. With zero ratings in the bucket no threshold is low enough. The library
already names this trap for the miss bound — "a model kept out of first place
produces no successful rows either" — and the quality window has the same shape
with no such compensation. Invariant 5 is what keeps it that way, and it should
stay: the alternative is synthetic scores, which it exists to forbid.

## Roughly how

**Two numbers, one comparison.** Keep the miss bound exactly as it is, semantics
included. Add an observed latency per model, living on its own clock — a success
does not retire it, because a success is what produces it. At slot acquisition
the existing term compares the caller's remaining budget against the **larger of
the two**. Still ordering-only, still budget-relative, still never a withdrawal,
still one read of the journal tail.

**Keyed by model name, not by `(model, operation)`.** The bound it feeds is keyed
by model alone, and splitting the latency per operation would make the two
diverge for no gain. Coarse is the default here.

**The number a stream contributes must not be the consumer's.** `latency_ms` is
taken when the journal row is written, and for a stream that is after the
consumer finished pulling — or abandoned the generator. So it measures the
consumer as much as the model, which is precisely the coupling `_stream_deltas`
deliberately broke when it stopped bounding anything past the first delta. Worse,
an abandoned slow stream journals a *short* latency and would teach the pool that
the model is fast — the exact opposite of the truth.

So record **time to the first delta** as its own journal field, and take the
observed latency from it on the streaming path and from the full time on the
non-streaming path, where no consumer can inflate it. This introduces no new
concept: `call-path.md` already states that for a stream `wait` bounds the wait
for the first delta, and the same split is already the settled semantics for
misses. One field on `Call` and in the three schemas is what invariant 13 exists
to permit.

**`_finish_ok` stops clearing what it should not.** Clearing the *miss* bound on
success stays. What must not happen is the same success erasing the observed
latency, and an abandonment must not be recorded as if the model had delivered.

## Recorded decisions and rules this touches

- [`budget-expiry-teaches-ordering`](../reference/decisions.md#budget-expiry-teaches-ordering)
  keeps its mechanism and its reasoning. Its premise is narrower than the
  problem: "a model that never answers produces no successful rows, so this is
  the only obtainable latency evidence" leaves out the model that answers slowly,
  which produces successful rows *and* the latency on them.
- **[`selection.md`](../reference/rules/selection.md) states that same premise in
  its own words** ("the only evidence obtainable... its latency cannot be measured
  any other way"). It becomes false with this change and is corrected in the same
  batch, not only the decision entry.
- [`no-bandit-machinery`](../reference/decisions.md#no-bandit-machinery) blocks
  "latency ranking", on the grounds that a chronically failing model is already
  disabled by exponential cooldown. That reason does not reach this case:
  nemotron never failed — it answered every time and took 36-57 s doing it.
  Cooldown disables failure; nothing notices slowness that succeeds. The entry is
  **narrowed, not opened**: a global speed ranking independent of the caller's
  budget stays blocked, as do e-exploration, usable-rate floors and
  auto-retirement. Leaving it unnarrowed would read as removed.
- Invariant 5 is the fence: observed latency may **never** enter the quality
  window. It feeds the ordering term and nothing else.
- Invariant 7 and
  [`latency-budget-per-call`](../reference/decisions.md#latency-budget-per-call)
  stay intact — no per-model timeout knob appears anywhere.
- The bound is **not** node-local, and this plan must not describe it as if it
  were: it is derived from the journal and so takes other processes' rows, which
  is a deliberate difference from a cooldown (invariant 11).

## The cost of the first-delta choice, and what it forces

Time to the first delta catches z.ai — 31 seconds before a token — and does
**not** catch nemotron, which opens promptly and then dribbles for a hundred
seconds. Nemotron is the headline row of the table above, so either this plan
also needs an "the answer reached its end" marker in the journal, or that case
belongs to Plan 2. **Plan 2 is the better answer**, because past the first delta
nothing else stops a trickle anyway; the marker is the fallback if detailed
planning finds Plan 2 too large. Either way the two ship together.

## For detailed planning

Which statistic over the observed latencies (a median is the coarse default, and
[`size-is-part-of-the-mission`](../reference/decisions.md#size-is-part-of-the-mission)
argues for the coarse one); how long an observed latency lives, given that the
ten-minute miss window exists to let a *single* observation expire and this one
accumulates; whether the two numbers combine as a maximum or as one derived
bound; and the wording of the amendments to the two entries and to
`selection.md`.

---

# Plan 2 — a stall timeout on a stream

**Superseded — implement [`stream-stall-timeout.md`](stream-stall-timeout.md), not
this section.** What follows is the reasoning that produced it, kept for the record.

**Ships with Plan 1.** It stops being optional the moment latency is learned from
first-delta timing, because that timing is blind to the trickle.

**What.** Let a caller bound the gap *between* deltas, so a stream that opens
promptly and then dribbles can be abandoned — and, per Plan 1, abandoned in a way
the pool learns from rather than one it mislearns from.

**Why.** A budget stops binding at the first delta, deliberately, so that a slow
consumer cannot trip it. Measured consequence: a 45-second budget returned a
101-second answer. A consumer can wrap its own timeout around the iteration, and
llmbroker already treats consumer-side abandonment as a completed call rather
than a model failure — but on its own that is exactly the mislearning Plan 1
describes.

**Recorded decisions this touches.** Invariant 7 and
[`latency-budget-per-call`](../reference/decisions.md#latency-budget-per-call)
permit this in one shape only: the value belongs to the **call**, never to the
model or the entry. An inter-delta gap rather than an absolute deadline is what
keeps the original reason for dropping the deadline intact — a consumer that
stops reading for a while is not a model that stopped answering.

---

# Plan 3 — a rate-limit streak ages

**What.** Give the consecutive-429 streak a decay on the wall clock: a streak
whose last failure is older than some multiple of the base is spent.

**Why.** The streak resets on success and on nothing else, and a model is not
tried while it is cooling, so it cannot succeed, so the streak only grows — and
the cooldown grows with it, doubling to the cap.

**This is narrow, and the measurements bound it.** Across a burst that cooled the
highest-weighted model 16 times in seven minutes, *every* cooldown stayed at the
flat 60-second base: the exponent never grows while a model keeps answering
between rate-limit hits, because each success resets the streak. The missing
decay does not bite a model under ordinary load. It bites a model that rarely
succeeds — the pool's z.ai endpoint answered 2 attempts of 6 in one minute,
refused 26 consecutively twenty minutes later, then answered again, all on HTTP
429 carrying **no `Retry-After`**. Driving that endpoint alone through the pool
shows it directly: 8 requests, 1 answered, the cooldown climbing 60 -> 120 -> 240
-> 480 seconds with the streak reaching 4. The same run shows the documented rule
holding correctly — a failed attempt made *while* the model was already cooling
did not advance the exponent.

**Only the clock-decay option is worth planning.** Capping the exponent when the
provider sent no `Retry-After` sounds equally local and is not: the absence of
the header does not survive to the decision point, because `retry_after_seconds`
substitutes a default and everything downstream sees a plain number. That would
mean threading a new signal through classification and disposal. The decay, by
contrast, is local to the optimizer's counter and heals by itself, as a cooldown
does.

**Recorded decisions this touches.** None blocks it.
[`no-rate-limits`](../reference/decisions.md#no-rate-limits) blocks *tracked
caps*, which this is not. The cooldown rules in
[`selection.md`](../reference/rules/selection.md) state the current behaviour and
are updated with it.

---

# Plan 4 — a reachability check

**The shape is settled.** It could not be written against "walk the configured
entries": the CLI reads presets and knows nothing of a registry, a store or a
connection string — that configuration surface does not exist there. So it is one
checking function taking model configs and a key resolver, with two thin wrappers:
the CLI passing a preset with env-backed secrets, a host passing its own.

**What it reports,** per row: no key / key refused / model refused / answered, and
how long it took. Read-only — it writes no registry row, no journal row, and
feeds no routing state.

**Why.** Neither `list` nor `env` answers whether a key works, and `snapshot()`
cannot either, because only a request settles it. The downstream project wrote
this by hand before it could start, and the result was not cosmetic: it showed
that one provider's "insufficient balance" error came only from its *paid* models
while the free one was reachable — the opposite of what the error text invites,
and a diagnosis nothing in the library would have corrected.

**Recorded decisions this touches.** Two are adjacent and neither blocks it; the
detailed plan should say so out loud.
[`no-alerts-api`](../reference/decisions.md#no-alerts-api) rejects a *runtime*
events API, where this is a command a human runs. The rejected
["proving a model dead before removing it"](../reference/decisions.md#rejected)
rejects probing that *decides* something, where this only reports to a person.

---

# Out of band — one line in the paid catalog

**Done.** The refresh prompt was re-run and the catalog now carries `gpt-5.6-luna`
as `gpt-fast`, and Anthropic's fast tier as `haiku`. The curation rule that had
dropped them — pick one to three models *on quality* — was widened first, so speed
is now a tier the refresh must consider and report on, and that rule is recorded in
[`../reference/decisions.md`](../reference/decisions.md#speed-is-a-catalog-tier).
The free-tier note at the end of this section is **still open**.

`gpt-5.6-luna` is absent from the curated paid catalog while OpenAI offers it. On
45 items across three languages it reached first delta in 5.5-6.3 s against
`gpt`'s 12-20 s, and a whole answer in 8.6-9.7 s against 19-27 s, at 100%
contract compliance and within 0.1-0.3 of `gpt` on a 1-5 quality rubric — level
with it on the one axis that decided anything downstream.

This is a catalog edit, not a plan, and it needs no second axis in the catalog:
an alias is a provider's model name, and the labels already speak about speed
where speed is what a model is for. The refresh prompt beside the catalog is the
route.

**Worth a look while there:** the free-tier list carries `glm-4.7-flash`, whose
entry is correct — the model exists, the key reaches it, and it answers through
the pool. Whether an endpoint that answers one request in eight, and takes 31
seconds to its first token when it does, should hold a place in a five-model pool
is a curation question, not a code one.

# Draft: what an interactive caller still needs

**Status: draft, deliberately not queued.** There is no row for it in
[`README.md`](README.md), so nothing picks it up. It says what to implement, why,
and roughly how; the work order, the tests and the spec moves belong to detailed
planning.

Two of its plans — latency in the fallback order, and a caller's bound on the gap
between deltas — were detailed, implemented and withdrawn unreleased; what a
streaming caller's budget covers instead is settled in
[`../reference/decisions.md`](../reference/decisions.md#a-budget-is-provider-time-not-wall-clock),
and the reasons they were dropped are in that file's rejected list. The catalog
edit has shipped. Plan 4 and the curation question have been queued as detailed plans
([`README.md`](README.md)); **Plan 3 below is the one thing still only a draft**, and
the queue's note says why it was not taken up.

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

# Still open — a free-tier curation question

The free-tier list carries `glm-4.7-flash`, whose entry is correct: the model
exists, the key reaches it, and it answers through the pool. Whether an endpoint
that answers one request in eight, and takes 31 seconds to its first token when it
does, should hold one of five places in a curated pool is a curation question, not
a code one.

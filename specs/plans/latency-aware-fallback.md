# Draft: what an interactive caller still needs

**Status: draft, deliberately not queued.** There is no row for it in
[`README.md`](README.md), so nothing picks it up: it says what to implement, why, and
roughly how, and leaves the work order, the tests and the spec moves to detailed
planning. Five items, in descending order of value; they are independent and may
well become separate plans.

## Where the evidence comes from

A downstream application (a vocabulary tool: one user, a couple of dozen words a
day, an answer streamed into a browser) benchmarked the free pool and the paid
direct client on ~40 items per source language across three languages, in two load
profiles — a burst of four concurrent requests, and a paced single-user session.
Numbers quoted below are from that run.

The headline is that the pool is excellent at what it is for: paced, a single user
got 20 answers out of 20 from the highest-weighted model, first delta 0.8 s, whole
answer under 2 s, no failover at all. Everything below is about the edges around
that.

---

## 1. The fallback order ignores latency

**What.** Widen the evidence behind the existing budget-relative ordering term:
today it learns only from budgets that *expired*, so it should also learn from how
long answers that *arrived* actually took.

**Why.** Under the burst profile the pool spilled over to its second and third
choices, and those are the slowest endpoints it has:

| answering model | curated weight | median whole answer |
|---|---|---|
| `google-gemini-3.5-flash-lite` | 0.75 | 1.6–1.7 s |
| `openrouter-nemotron-3-ultra` | 0.72 | 35–57 s (worst 101 s) |
| `openrouter-laguna-s-2.1` | 0.70 | 43 s |
| `groq-gpt-oss-120b` | 0.55 | 2.8–3.5 s |

The two slowest carry the highest weights after the primary, so the *first*
fallback is the worst possible choice for an interactive caller, and the fastest
alternative sorts last. The pool answers — 20 to 60 times slower than it needed to.

The existing term cannot catch this. A budget bounds queueing and the first delta;
past the first delta the answer is unbounded (invariant 18). A model that opens
promptly and then trickles for 101 s therefore never expires a budget, journals a
successful row, and keeps its curated position forever. The one case the ordering
term was built for — the model that answers *nothing* in time — is not the case that
actually hurts.

**Roughly how.** `Learner.relearn()` already reads the journal tail once and derives
the quality windows and the budget bounds from it; a successful row already carries
its own latency. Derive an observed latency per `(model, operation)` from that same
read, and let it raise the same bound the expiries raise. Nothing else moves: still
ordering-only, still budget-relative, still soft, still node-local, still one tail
read. A caller offering no budget is unaffected; when every candidate is over the
budget the term is equal for all and curated order stands.

**Recorded decisions this touches — and where it contradicts one.**

- [`budget-expiry-teaches-ordering`](../reference/decisions.md#budget-expiry-teaches-ordering)
  is the mechanism being widened, not replaced. Its reasoning holds verbatim; only
  its premise — "a model that never answers produces no successful rows, so this is
  the only obtainable latency evidence" — turns out to be narrower than the problem.
  A model that answers slowly produces successful rows *and* the latency on them.
- [`no-bandit-machinery`](../reference/decisions.md#no-bandit-machinery) blocks
  "latency ranking" outright, on the grounds that *a chronically failing model is
  already effectively disabled by exponential cooldown*. **That reason does not
  reach this case, and the measurements are what show it:** nemotron never failed.
  It answered, successfully, every time, taking 35–57 s about it. Cooldown disables
  failure; nothing in the library today notices slowness that succeeds. The entry's
  other blocks — ε-exploration, usable-rate floors, auto-retirement — stay blocked,
  and this proposal introduces none of them: no global ranking, no exploration, no
  withdrawal, one number derived from a read that already happens.
- Invariant 5 is the fence this must not cross: observed latency may **never** enter
  the quality window, which takes host ratings only. It feeds the budget-bound term
  and nothing else.
- Invariant 7 and
  [`latency-budget-per-call`](../reference/decisions.md#latency-budget-per-call) stay
  intact: no per-model timeout knob appears anywhere.

**For detailed planning.** Which statistic (a median is the coarse default, and
[`size-is-part-of-the-mission`](../reference/decisions.md#size-is-part-of-the-mission)
argues for the coarse one); whether an observed latency and an expiry write the same
bound or two that combine; how the existing ten-minute window applies to evidence
that arrives from a much longer tail; and whether either entry above is amended or a
new one is written.

---

## 2. A rate-limit streak never ages

**What.** Give the consecutive-429 streak a time-based decay, or bound its exponent
for providers that send no `Retry-After`.

**Why.** The streak resets on success and on nothing else. A model is not tried while
it is cooling, so it cannot succeed, so the streak only grows — and the cooldown grows
with it, doubling to the cap. A provider whose free tier flaps on a seconds timescale
gets parked for far longer than it was ever unavailable.

Measured on the pool's z.ai endpoint: HTTP 429 carrying **no `Retry-After`** (so the
flat base is used), answering 2 attempts out of 6 in one minute, then refusing 26
consecutive attempts twenty minutes later, then answering again. Six consecutive
misses are enough to park it for an hour.

The library already recognises this exact trap elsewhere and solves it:
`budget_bounds_from_calls` ages its evidence by a window precisely because "a model
never picked never succeeds, so nothing else would clear it". The streak is the same
shape of state with none of that protection.

**Roughly how.** Either age the streak by wall-clock — a streak whose last failure is
older than some multiple of the base is spent — or cap the exponent when the provider
gave no `Retry-After`, on the grounds that without one the library is guessing anyway.
Both are small and local to the optimizer's counter.

**Recorded decisions this touches.** None blocks it.
[`no-rate-limits`](../reference/decisions.md#no-rate-limits) blocks *tracked caps*,
which this is not — the streak already exists. The cooldown rules in
[`selection.md`](../reference/rules/selection.md) state the current behaviour and
would be updated with it.

---

## 3. There is no way to ask "what actually works here?"

**What.** A diagnostic command — `llmbroker doctor` or similar — that tries one tiny
request per configured model and reports, per row: no key / key refused / model
refused / answered, and how long it took.

**Why.** `list` prints the curated lists and `env` prints the key names; neither
answers whether a key works, and `snapshot()` cannot answer it either, because the
question is only settled by making a request. The downstream project wrote exactly
this by hand before it could start, and the result was not cosmetic: it showed that
one provider's "insufficient balance" error came only from its *paid* models while
the free one was reachable, which is the opposite of the conclusion the error text
invites. Without it, an operator debugging a quiet pool has log lines and guesswork.

**Roughly how.** Walk the configured entries, resolve each `api_key_ref` through the
secrets port, issue one minimal completion per model, classify the outcome by the
same rules the router already uses, print a table. Read-only: it writes no registry
row, no journal row, and feeds no routing state.

**Recorded decisions this touches.** Two are adjacent and neither blocks it, but the
detailed plan should say so out loud:
[`no-alerts-api`](../reference/decisions.md#no-alerts-api) rejects a *runtime* events
API, where this is a command a human runs; and the rejected
["proving a model dead before removing it"](../reference/decisions.md#rejected) rejects
probing that *decides* something, where this only reports to a person.

---

## 4. The paid catalog has no fast tier

**What.** Add `gpt-5.6-luna` to the curated paid catalog, and consider whether the
catalog should distinguish models by speed the way it distinguishes them by strength.

**Why.** The catalog offers OpenAI as `gpt` (5.6-sol) and `gpt-mini` (5.6-terra). On
45 items across three languages, `gpt-5.6-luna` — absent from the catalog — reached
first delta in 5.5–6.3 s against sol's 12–20 s, and a whole answer in 8.6–9.7 s
against 19–27 s, at 100% contract compliance and within 0.1–0.3 of sol on a 1–5
quality rubric (level with it on the one axis that decided anything downstream). Sol
misses a 3–5 s first-content budget on every single call; luna nearly meets it.

The general point outlives the one model: the catalog's axis is strength
(`opus`/`sonnet`, `gpt`/`gpt-mini`), and a caller streaming into a UI chooses on
speed. Today it cannot express that choice through an alias at all.

**Recorded decisions this touches.**
[`the-paid-catalog-is-curated-too`](../reference/decisions.md#the-paid-catalog-is-curated-too)
is the reason the catalog exists and argues for keeping it complete. The refresh
prompt beside the catalog is the existing route for adding a model.

**Also worth a look while there:** the free-tier list carries `glm-4.7-flash`, whose
entry is correct — the model exists and the key reaches it — but whose measured
availability was near zero (see item 2). Whether an endpoint that rarely answers
should hold a place in a five-model pool is a curation question, not a code one.

---

## 5. Optional: a stall timeout on a stream

**What.** Let a caller bound the gap *between* deltas, so a stream that opens and then
trickles can be abandoned.

**Why.** A budget stops binding at the first delta — deliberately, so that a slow
consumer cannot trip it — which is why a 45 s budget returned a 101 s answer. This is
the weakest item on the list: a consumer can wrap its own timeout around the
iteration, and llmbroker already treats consumer-side abandonment correctly, as a
completed call rather than a model failure. If item 1 lands, the pool learns from
slowness without this.

**Recorded decisions this touches.** Invariant 7 and
[`latency-budget-per-call`](../reference/decisions.md#latency-budget-per-call) permit
it only in one shape: the value belongs to the *call*, never to the model or the
entry. An inter-delta gap rather than an absolute deadline is what keeps the original
reason for dropping the deadline intact.

---

## What detailed planning must settle across all five

Which of these ship together and which stand alone; for each contested call, the
[`decisions.md`](../reference/decisions.md) entry that lands in the same batch as the
behavior; the spec moves (items 1 and 2 both change what
[`selection.md`](../reference/rules/selection.md) states today); and the tests. The
gate is unchanged: `invoke pre` and `python -m pytest`, both green.

# Plan — a model that says nothing stops holding the pool

## Goal

A model that answers with silence for a caller's whole budget costs that caller
its answer, and then costs the next caller the same, because nothing about the
silence is remembered as a reason to try someone else first. Treat it the way
every other failed attempt is treated: cool the model down and move on.

## Why this is not the failure mode the queue warns against

No new mechanism. `_classify` already maps a failed attempt onto its disposal,
and every other failure shape reaching it carries a `cool_base`. This is one
branch of that function saying what the others already say.

## The defect

`_classify` maps a timeout inside the caller's budget to `_BudgetExpired()` with
no `cool_base`, on the reasoning that a spent wait budget is not the model's
fault. That reasoning fits a model that answered slowly. It does not fit one that
sent nothing at all: to every caller, an endpoint that produces no delta for the
whole budget is indistinguishable from a dead one, and the next request has no
reason not to pick it again.

Measured on a host with `wait = 25`, one word submitted, nothing else in flight:

```
16:05:32  openrouter-nemotron-3-ultra  error  lat=25007  budget=24999  wait budget exhausted
16:05:32  openrouter-nemotron-3-ultra  error  lat=25003  budget=24999  wait budget exhausted
```

The pool also held `groq-gpt-oss-120b`, answering in 0.4–4.1 s all afternoon, and
`google-gemini-3.5-flash-lite`, refusing in under a second — a refusal that costs
nothing and moves on. Neither was reached: the budget was gone. Every request that
day fell through to the host's paid step, which is the outcome a pool exists to
avoid, and the host reported the application as unusable.

**What already exists, and why it is not enough.** `Pool._over_budget` prefers a
sibling over a model that recently missed a budget as small as the one on offer.
The instinct is right and the coverage is not: it is a preference and never an
exclusion, so it yields nothing once the siblings are cooling; its window retires
the evidence after `BUDGET_BOUND_WINDOW_SEC`, and the host's requests arrived
further apart than that; and it is recorded only after a call has already spent
the budget, so two calls issued together — an interactive host commonly makes two
per user action — both choose the silent model before either has learned anything.
A cooldown is what the second and third of those need, and it is what the rest of
the router already does with a failure.

## What to build

1. **Silence for the whole budget cools the model.** In `_classify`, a
   budget-bound timeout with no delta produced gets a `cool_base` like any other
   failed attempt, and the caller still receives its own expired `wait`.
2. **A model that started answering is untouched.** The router already separates
   "the wait budget ran out before any LLM produced a delta" from "answer budget
   exhausted after ...". Only the first is silence; the second is a caller's
   budget ending over an answer that was arriving, and cooling for that would
   punish a model for the caller's setting.
3. **The learned budget bound stays.** It answers a different question — who to
   prefer at a comparable budget — and the two do not overlap once silence also
   cools.

## What stays out

- **A per-attempt share of the budget.** It rescues the request in flight, and it
  is a second mechanism aimed at a symptom this one already removes for every
  request after the first. Not worth the machinery.
- **Cooling for a slow answer.** Explicitly kept out by item 2.
- **Changing what `wait` means to a caller.** It still bounds the whole answer in
  provider time.
- **A decay on the rate-limit streak.** Weighed and unqueued; `README.md` states
  the condition under which it returns.

## Tests

- A model yielding no delta inside a bounded wait is cooled, and the next call in
  the same pool reaches a sibling.
- A model whose first delta arrives inside the budget and whose answer then runs
  past it is not cooled.
- The caller still sees its own expired wait, unchanged in type and message.
- The cooldown is journaled with the row, as every other cooldown is.

## What moves into the specs

That silence for a whole budget is a failure like any other, while a slow answer
is not, belongs with the routing rules and is stated once. It names the
alternative it rejects — treating both as the caller's own setting — so
`decisions.md` is the file.

## Gate

`invoke pre` and the full suite green. No provider key and no network: a fake
provider that never yields is enough to pin every case above.

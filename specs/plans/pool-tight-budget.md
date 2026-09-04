# Plan — a model that says nothing stops holding the pool

**Status: source-bound; ready to hand off.** The module ownership, behavior split,
test edits and spec replacement below were checked against the current router. If
an earlier diff changes the router before this is executed, revalidate them first.

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

## Source binding

The behavior is owned by `src/llmbroker/broker/router.py`; no pool, optimizer,
model or store protocol change is needed.

- `_classify` already knows both facts needed for a pre-answer expiry: the
  exception is a timeout and the active attempt was bounded by the caller's
  remaining budget. Its budget-expiry verdict keeps `_BudgetExpired` as the
  routing outcome and also receives the ordinary default cooldown base.
- `_dispose` must treat the two facts independently. `_BudgetExpired` means
  record `budget_ms` and raise the live budget bound. `cool_base` means cool the
  slot; its absence means release it. The current `if/else` makes those actions
  mutually exclusive, so merely adding `cool_base` in `_classify` would silently
  stop journaling the bound. Reshape this method before changing the verdict.
- `_spent_budget` stays separate. It is reached when the deadline was already
  gone before the provider request opened, so it releases the slot, records no
  budget bound and never cools the model.
- `_exhausted` keeps constructing `_BudgetExpired` without a cooldown base. That
  is the streaming path after the first delta: it still records and raises the
  latency bound, releases the slot, and never cools.
- `_route` is unchanged: once the caller's whole budget is gone, the current call
  still ends with the same timeout rather than trying a sibling with no time left.
  The cooldown benefits the following caller.

The cooldown uses the same `_DEFAULT_RATE_LIMIT_SEC`, `_capped_wait`, pool
`cool_down`, learner observation and journal representation as a transport
failure. Do not introduce a timeout-specific duration or a new status.

## Work order

1. In `tests/test_wait_budget.py`, turn the existing completion timeout case into
   the positive contract: the caller still gets `NoLLMAvailableError` with
   `reason == "timeout"`, while the attempted model is cooling, its failure count
   advanced, and its row carries both a cooldown and the missed budget.
2. In `tests/test_router_stream.py`, make the existing before-first-delta timeout
   assert the same cooldown and journal facts. Keep the exception assertion
   unchanged.
3. In `tests/test_budget_ordering.py`, preserve the existing next-caller
   regression but change its reason: the sibling is reached because the first
   model is cooling. Assert that the same row still derives a budget bound, so
   availability did not erase ordering evidence.
4. Keep the negative boundaries explicit in
   `tests/test_whole_answer_budget.py` and `tests/test_wait_budget.py`: a stream
   that emitted a delta before expiring is not cooled; a budget spent before the
   provider was opened is not cooled and teaches no bound; a global HTTP ceiling
   remains the existing provider-failure path rather than a budget expiry.
5. Reshape `Router._dispose` so budget evidence and slot disposal are orthogonal,
   then add the cooldown base to the pre-answer budget-timeout branch in
   `_classify`. Make no public signature or exception change.
6. In the same batch, replace the stale budget wording in
   `specs/reference/rules/call-path.md`,
   `specs/reference/rules/selection.md`, and the decision entry quoted below.
7. Run the focused files from steps 1–4, then the full gate. Skip the version
   bump; the maintainer does it by hand.

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
- The cooldown for silence is the ordinary base: a model cooled for it is
  selectable again as soon as any other model cooled by a transport failure would
  be, and repeated silence escalates only through the existing streak, never
  through a duration of its own.

The focused regression command after the implementation is:

```console
python -m pytest tests/test_wait_budget.py tests/test_router_stream.py \
  tests/test_whole_answer_budget.py tests/test_budget_ordering.py
```

## What moves into the specs

That silence for a whole budget is a failure like any other, while a slow answer
is not, belongs with the routing rules and is stated once. It names the
alternative it rejects — treating both as the caller's own setting — so
`decisions.md` is the file.

Replace the current `budget-expiry-teaches-ordering` entry with this entry in the
same batch as the behavior:

> ### silence-cools-and-teaches-ordering
>
> A caller budget expiring before a model produces an answer is two facts: the
> model was unavailable to that caller, so it is cooled like another failed
> attempt; and it failed to answer within that budget, so the budget is journaled
> and teaches ordering. An expiry after the first streamed delta teaches only the
> latter. An expiry before the provider was reached teaches neither.
>
> **Blocks:** treating every expiry as only the caller's fault; treating every
> expiry as a model failure; making cooldown and latency evidence mutually
> exclusive; discarding the latency bound once silence also cools; reading this as
> "a slow start is a dead endpoint".
> **Why:** to a caller, a model that produces nothing for the whole budget is
> indistinguishable from a dead endpoint, and without cooldown the next request
> pays the same whole budget. Once output has arrived, the opposite is known: the
> model answered and the caller merely stopped waiting for the rest, so cooling it
> would withdraw a working model. The recorded bound remains useful after the
> short cooldown and on another process, while cooldown protects the immediate
> next calls in this process; neither replaces the other.
>
> **The measurement is one caller's and the withdrawal is everyone's.** Silence is
> measured against the budget of the caller that met it, while the cooldown removes
> the model from the whole process — including a caller whose budget is twice as
> long and for whom that model answers well. Two things keep that proportionate,
> and both are part of this entry rather than incidental to it: the duration is the
> ordinary base, with no timeout-specific value, so a model that merely starts
> slowly for a tight budget is back within it; and ordering by the learned bound
> continues to hold afterwards, which is the mechanism that is actually budget-aware.
> A model whose first delta is simply later than one caller's budget is not withdrawn
> from the pool in any lasting sense, and must not be made so by lengthening this.

Update links to the old anchor in the same batch. In `call-path.md`, replace the
absolute statement that a spent budget never cools with the three-way split above.
In `selection.md`, keep the bound's current lifetime and ranking rules, but stop
claiming that every expiry is availability-neutral. No cross-cutting invariant
changes.

## Gate

`invoke pre` and the full suite green. No provider key and no network: a fake
provider that never yields is enough to pin every case above.

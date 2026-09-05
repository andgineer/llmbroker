# Implementation plans

## Queue

| # | plan | readiness | what it is |
|---|---|---|---|
| 1 | [`caller-surface.md`](caller-surface.md) | functional; concretize now | what a caller can send and which models it can name: request parameters on a model reached by name, programmatic catalog access, and schema-constrained output routed through the pool behind its own probe |
| 2 | [`load-harness.md`](load-harness.md) | source-bound; revalidate after row 1 | the reusable half of a downstream harness, so a controlled pair — one variable moved, everything else held — can be taken here instead of inside one host's private script |
| 3 | [`caller-visibility.md`](caller-visibility.md) | functional; concretize after row 2 | what a caller can see of a call it made: usage from a stream, journal rows for direct calls, whether any output reached the reader, and latency in the derived aggregates |

## Detail horizon

The queue is ordered work, not three implementation briefs that may be handed to
three executors at once. Only a **source-bound** row is executable. A **functional**
row preserves the problem, boundary and evidence, but deliberately leaves names,
signatures and test placement open until every earlier handover has landed.

Concretization is rolling:

1. Before handing off the first row, bind it to the current modules, call paths,
   tests and spec edits. That binding lives in the plan itself.
2. After its implementation and review, read its `Handover`, inspect the new source
   and re-evaluate the rest of the queue. Remove work the code already did, split or
   combine items whose seams moved, and only then make the next row source-bound.
3. A later row that is already source-bound is still revalidated, not rewritten in
   advance. If its named surface still exists and the preceding diff did not change
   its assumptions, the revalidation is a small check rather than a new planning
   pass.
4. Hand off one source-bound row at a time. Do not ask an executor to fill in a
   functional row while implementing it: the unresolved public shape is planning
   work, and discovering it inside the implementation diff makes review impossible.

This keeps durable detail now — dependencies, outcome, exclusions and acceptance
evidence — while postponing volatile detail: private helper names, exact signatures
and test locations beyond the next implementation boundary.

## Rejected proposals

Two proposals were rejected rather than implemented, and the reasoning is worth keeping
because both will be re-proposed otherwise. A *reachability check* — a read-only,
human-run command reporting whether each key reaches its model — was a module, a CLI
verb and a permanent public function for a one-time onboarding act on a handful of
providers; what was actually valuable in it was knowledge, and that already lives in
[`../reference/freetier-providers.md`](../reference/freetier-providers.md). A *routing-worth
rubric* — measured availability and time-to-answer entering the curated weight — was a
rule, a recorded decision and a third copy of a rubric, to reorder five rows: the one
endpoint it was written against now carries the floor weight, stated where the evidence
for it already was, and the rest of it bought nothing.

Both are the same failure mode, and it is the one to check a new plan against: a
mechanism sized for a problem this pool does not have
([`../reference/mission.md`](../reference/mission.md#the-size-of-the-problem)).

The rate-limit streak decay stays weighed and unqueued, and the reason has moved.
A tight caller budget does drive the backoff exponent — the pair in
[`../../bench/runs/cooldowns.md`](../../bench/runs/cooldowns.md) shows 60 → 480 s at
a 25 s budget where 45 s stayed flat, and a host in ordinary interactive use recorded
59 → 1919 s across one day. But a model leaving the pool for half an hour only empties
the pool when the models left behind cannot be reached, and that half is answered:
silence for a whole budget now cools the endpoint that produced it, so the models
behind it are reached
([`../reference/decisions.md`](../reference/decisions.md#silence-cools-and-teaches-ordering)).
What is left is whether a caller still misses answers the pool could have given, and
that question needs a controlled pair, which is queue row 2. A second mechanism aimed
at the same symptom before it is the failure mode above.

A queued plan is a row in this file and a file beside it. How one is executed lives in `CLAUDE.md`
under "Executing a plan", and the rules binding on every plan live in
[`../reference/invariants.md`](../reference/invariants.md), which is loaded for every task. Neither
is restated here.

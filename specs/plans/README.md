# Implementation plans

## Queue

| # | plan | what it is |
|---|---|---|
| 1 | [`pool-tight-budget.md`](pool-tight-budget.md) | a model that produces no delta for the caller's whole budget is cooled like any other failed attempt, so one silent endpoint stops emptying the pool for every request after it |
| 2 | [`caller-surface.md`](caller-surface.md) | what a caller can send and which models it can name: request parameters on a model reached by name, programmatic catalog access, and schema-constrained output routed through the pool behind its own probe |
| 3 | [`load-harness.md`](load-harness.md) | the reusable half of a downstream harness, so a controlled pair — one variable moved, everything else held — can be taken here instead of inside one host's private script |
| 4 | [`caller-visibility.md`](caller-visibility.md) | what a caller can see of a call it made: usage from a stream, journal rows for direct calls, whether any output reached the reader, and latency in the derived aggregates |

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
the pool when the models left behind cannot be reached, and that is queue row 1.
Revisit after it lands, on whether a caller still misses answers the pool could have
given — and that question needs a controlled pair, which is queue row 3. A second
mechanism aimed at the same symptom before either is the failure mode above.

A queued plan is a row in this file and a file beside it. How one is executed lives in `CLAUDE.md`
under "Executing a plan", and the rules binding on every plan live in
[`../reference/invariants.md`](../reference/invariants.md), which is loaded for every task. Neither
is restated here.

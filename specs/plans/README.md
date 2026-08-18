# Implementation plans

**The queue is empty.** Nothing is waiting to be implemented.

Two plans were dropped rather than implemented, and the reasoning is worth keeping
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

[`latency-aware-fallback.md`](latency-aware-fallback.md) is a draft, not a queued plan.
What is still live in it is the rate-limit streak decay, which the queue deliberately
does not carry: the measurements in that same file show every cooldown of a model under
ordinary load staying at the flat base, and the exponent growing only on an endpoint that
refuses most requests — where growing is the defence working, not a defect. That endpoint
is answered by the floor weight it now carries in the curated list, at no runtime cost.

A queued plan is a row in this file and a file beside it. How one is executed lives in `CLAUDE.md`
under "Executing a plan", and the rules binding on every plan live in
[`../reference/invariants.md`](../reference/invariants.md), which is loaded for every task. Neither
is restated here.

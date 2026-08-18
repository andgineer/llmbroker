# Implementation plans

## Queue

| # | plan | what it is |
|---|---|---|
| 1 | [`load-harness.md`](load-harness.md) | a script beside the library that drives real traffic through the pool or a direct client and reduces it per model — no `src/` change, outside CI |

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

The rate-limit streak decay — a streak whose last failure is old enough to count as
spent — is weighed and not queued, and the reason it is not queued is now known to be
conditional. Every cooldown of a model under ordinary load staying at the flat base —
the exponent growing only on an endpoint that refuses most requests, where growing is
the defence working rather than a defect — holds at a loose caller budget and fails at
a tight one: on the same prompts and the same concurrency, with only the budget moved
from 45 s to 25 s, the highest-weighted model climbed the ladder to 480 s and two
others reached 1920 s and the 3600 s cap
([`../../bench/runs/cooldowns.md`](../../bench/runs/cooldowns.md)). A tight budget is
what an interactive caller is told to set. Whether that changes the verdict needs the
pair run again deliberately, which is what plan 1 above is for. If it does, the shape
is a decay on the streak counter rather than a cap on the exponent: a provider's
silence about `Retry-After` does not survive to the decision point, where a default
number has already replaced it.

A queued plan is a row in this file and a file beside it. How one is executed lives in `CLAUDE.md`
under "Executing a plan", and the rules binding on every plan live in
[`../reference/invariants.md`](../reference/invariants.md), which is loaded for every task. Neither
is restated here.

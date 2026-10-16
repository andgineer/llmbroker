# Implementation plans

## Queue

| # | plan | readiness | what it is |
|---|---|---|---|
| 1 | [`release-pipeline.md`](release-pipeline.md) | source-bound on `095c469ff`; CI workflows only, independent of the rest | the `github-pages` concurrency group cancels CI, and the publish gate then skips the release — v1.10.2 and v1.10.4 never reached PyPI |
| 2 | [`architecture-simplification.md`](architecture-simplification.md) | source-bound on `67659c350`; revalidate against the working tree first | consolidate stream ownership and attempt settlement, remove duplicate rebuilds, acknowledge journal writes, simplify deadlines and backend rating storage; retain the alternative-answer contract |
| 3 | [`caller-visibility.md`](caller-visibility.md) | functional; concretize after row 2 | what a caller can see of a call it made: usage from a streamed direct call, journal rows for direct calls, whether any output reached the reader, and latency plus the learned latency bound in what the broker reports |

Architecture simplification was bound before several changes landed on its ground: tools and
tool parameters on direct calls, a broker whose context provisions nothing while `direct()`
ticks the refresh clock, per-handle resolution of declared models, the reason a stream
reports when no model can serve it, and a synchronous stream facade that drives the async
handle. They reshape the refresher, the direct clients, the request builder, the handle it
keeps public and the stream's error path it consolidates, so its binding is revalidated
against the current source before any batch is handed off.

Architecture simplification comes before caller visibility because it rewrites the three
places caller visibility adds to — the stream handle, the journal's call rows and the journal
read the aggregates derive from — so visibility is designed once, on settled contracts. Its
batches form one implementation and review unit; complete all of them before concretizing
caller visibility. No plan requires a joint release with another.

## Detail horizon

The queue is ordered work, not a set of implementation briefs that may be handed to
several executors at once. Only a **source-bound** row is executable. A **functional**
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

## Unmeasured

An explicitly raced stream settles on the first complete answer rather than the first
delta. Its only evidence is one host's uncontrolled runs; no controlled comparison of the two
settlements exists. Settling it takes a controlled pair — the same prompts, one variable
moved, everything else held — taken by a host's own harness, not here. The pair compares
completion rate inside the caller's budget, which model ends up authoritative, host-rated
quality, visible first-delta latency, replacement frequency, whole-answer latency and
duplicate provider usage. It records every lane's timings, not only the winner's: they replay
the replacement rate at candidate values of the caller's selection interval, the one number
that settles that interval's default
([`../reference/decisions.md`](../reference/decisions.md#parallelism-is-explicit-or-recovery-owned)).

## Not queued

Every new plan is checked against one failure mode: a mechanism sized for a problem this
pool does not have ([`../reference/mission.md`](../reference/mission.md#the-size-of-the-problem)).
These fail it, or wait on evidence first:

- **Separate APIs for ordinary streams and streams with alternative answers** — see
  [`streamed-alternatives-live-until-close`](../reference/decisions.md#streamed-alternatives-live-until-close).
- **A reachability check** (a read-only command reporting whether each key reaches its
  model) — a module, a CLI verb and a permanent public function for a one-time onboarding
  act on a handful of providers. The knowledge it produces lives in
  [`../reference/freetier-providers.md`](../reference/freetier-providers.md).
- **A routing-worth rubric** (measured availability and time-to-answer entering the curated
  weight) — a rule, a recorded decision and a third copy of a rubric to reorder five rows.
  The one endpoint it targets carries the floor weight, stated next to its evidence.
- **A rate-limit streak decay** — waits on evidence. A tight caller budget does drive the
  backoff exponent: [`../../bench/runs/cooldowns.md`](../../bench/runs/cooldowns.md) shows
  60 → 480 s at a 25 s budget where 45 s stays flat, and a host in interactive use reaches
  59 → 1919 s in a day. A model out for half an hour empties the pool only if the models
  behind it cannot be reached, and they can: silence for a whole budget cools the endpoint
  that produced it
  ([`../reference/decisions.md`](../reference/decisions.md#silence-cools-and-teaches-ordering)).
  The open question is whether a caller still misses answers the pool could have given; it
  needs a controlled pair before any mechanism is aimed at it.

A queued plan is a row in this file and a file beside it. How one is executed lives in `CLAUDE.md`
under "Executing a plan", and the rules binding on every plan live in
[`../reference/invariants.md`](../reference/invariants.md), which is loaded for every task. Neither
is restated here.

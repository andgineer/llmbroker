# Implementation plans

## Queue

| # | plan | readiness | what it is |
|---|---|---|---|
| 1 | [`release-pipeline.md`](release-pipeline.md) | source-bound on `095c469ff`; CI workflows only, independent of the rest | the `github-pages` concurrency group cancels CI, and the publish gate then skips the release — v1.10.2 and v1.10.4 never reached PyPI |
| 2 | [`host-gaps.md`](host-gaps.md) | source-bound on `7daec8c3c`; three independent items | what a host built around the library: a caller's budget that the pass after a key re-read overruns, a key re-read the synchronous broker does not pass through, and default secrets that cannot list the names they hold |
| 3 | [`futile-wait.md`](futile-wait.md) | source-bound on `7daec8c3c`; implement on top of row 2's handover | a call whose `wait` ends before any cooling model returns is held for the whole budget and then given the error that was known at the start; it ends at once instead |
| 4 | [`architecture-simplification.md`](architecture-simplification.md) | source-bound on `67659c350`; revalidate against the working tree first | consolidate stream ownership and attempt settlement, remove duplicate rebuilds, acknowledge journal writes, simplify deadlines and backend rating storage; retain the alternative-answer contract |
| 5 | [`caller-visibility.md`](caller-visibility.md) | functional; concretize after row 4 | what a caller can see of a call it made: usage from a streamed direct call, journal rows for direct calls, and latency plus the learned latency bound in what the broker reports |

Host gaps comes before the refactor because each of its items is a workaround standing in a
host today, each is small enough to review on its own, and none changes stored data. Its
one edit on the refactor's ground is the deadline the pass after a re-read runs under.

Futile wait follows host gaps because the early exit it adds leads straight into the pass
after a re-read, which is only bounded by the caller's deadline once host gaps item 1 is in.
It comes before the refactor for the same reason host gaps does: it is one check, visible
to every host with a fallback, and it changes no stored data.

Architecture simplification was bound before several changes landed on its ground: tools and
tool parameters on direct calls, a broker whose context provisions nothing while `direct()`
ticks the refresh clock, per-handle resolution of declared models, the reason a stream
reports when no model can serve it, and a synchronous stream facade that drives the async
handle. They reshape the refresher, the direct clients, the request builder, the handle it
keeps public and the stream's error path it consolidates, so its binding is revalidated
against the current source before any batch is handed off.

Architecture simplification comes before caller visibility because it rewrites the three
places caller visibility adds to — the stream handle, the journal's call rows and the journal
read the aggregates derive from — so visibility is designed once, on settled contracts. A journaled direct call also needs a
mark the stored row does not carry, and the refactor changes the stored row too; done in
this order a database host resets its journal once. Its batches form one implementation
and review unit; complete all of them before concretizing caller visibility. No plan
requires a joint release with another.

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

## What does not belong here

This directory holds work that is going to be done, and nothing else. Every new plan is
checked against one failure mode first: a mechanism sized for a problem this pool does not
have ([`../reference/mission.md`](../reference/mission.md#the-size-of-the-problem)). An idea
that fails it, or that waits on evidence nobody is gathering, is not parked here: it is
recorded once, with what would reopen it, in
[`../reference/decisions.md`](../reference/decisions.md#rejected), and has no file and no row.

A queued plan is a row in this file and a file beside it. How one is executed lives in `CLAUDE.md`
under "Executing a plan", and the rules binding on every plan live in
[`../reference/invariants.md`](../reference/invariants.md), which is loaded for every task. Neither
is restated here.

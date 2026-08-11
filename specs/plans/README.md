# Implementation plans

One plan is queued. **Take [`one-broker-four-triggers`](one-broker-four-triggers.md).**

**How a plan is executed** — the rules an agent follows when told to implement one — live in
`CLAUDE.md` under "Executing a plan": code wins over a stale plan, gate on `invoke pre` + `pytest`
after every batch, never bump the version, never commit unasked, and leave the plan file in place
for review. Nothing needs restating in the request.

## Order

| Plan | Status | Blocked by | Notes |
|---|---|---|---|
| [one-broker-four-triggers](one-broker-four-triggers.md) | queued | — | the deletion pass: the merge becomes a mirror, availability stops being shared, one broker per process with callers per request, the pool rebuilt on four triggers with keys riding them, ten rule files become five, then an audit against `git diff 1.3.0..HEAD` |

## What happened to the previous queue

Ten plans were queued here and all ten were deleted, along with the queue that ordered them. They
were written against a task understood as *a routing library that must also be correct as a
distributed system*, and they sized their mechanisms accordingly: exact propagation of one process's
findings to another, per-scope cache windows over a metered secrets backend, a merge that held a
hearing before removing an entry. The task is smaller than that, and
[`mission.md`](../reference/mission.md#the-size-of-the-problem) now says how much smaller, so those
plans could not be re-ordered into correctness — a plan whose premise is the wrong scale does not
improve by being taken later.

What survived did so as content, not as files: the object model of `one-broker-many-callers` is
batch 3 of the plan above, and the secrets enumeration of `a-key-is-asked-for-once` is part of batch
4, stripped of the two-window doctrine it was wrapped in.

Nothing here is a record of that reasoning — the decisions are in
[`decisions.md`](../reference/decisions.md#size-is-part-of-the-mission), which is where a future
proposal will look before re-proposing one of them.

The plans of released work are gone too, on the maintainer's word: a plan is a route, and once
travelled the code and its tests are the artifact. Everything durable those plans carried had
already moved into [`../reference/`](../reference/) in the batch that wrote it — verified before they
were deleted, entry by entry, against `decisions.md`.

## Standing rules for whatever is queued next

The rules binding on every plan live in [`../reference/invariants.md`](../reference/invariants.md),
which is loaded for every task. Nothing is restated here — a rule written twice is a rule that will
drift.

Two consequences worth naming for plan authors:

- A new persisted field on a registry entry joins the sync identity comparison automatically, so a
  plan that adds one owes it a test, not a mechanism.
- Before proposing a mechanism, check it against the scale the mission states. A plan whose
  justification is a load this pool cannot reach is the failure the previous queue is named after.

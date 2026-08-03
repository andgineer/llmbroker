# Implementation plans

Order for the plans queued now. A plan that is fully implemented and merged is deleted (anything
spec-worthy moves into `specs/` first) and its row here goes with it.

**How these are executed** — the rules an agent follows when told to implement one — live in
`CLAUDE.md` under "Executing a plan": take the first queued row unless a plan is named, code wins
over a stale plan, gate on `invoke pre` + `pytest` after every batch, never bump the version,
never commit unasked, and leave the plan file in place for review. Nothing needs to be restated in
the request. The plan and its row here are removed only after review and merge, on request.

Statuses as of 2026-08-03: the queue is empty.

## Order

| # | Plan | Issue | Blocked by | Notes |
|---|---|---|---|---|
| — | — | — | — | nothing queued |

## Standing rules for whatever is queued next

The rules binding on every plan live in [`../reference/invariants.md`](../reference/invariants.md),
which is loaded for every task. Nothing is restated here — a rule written twice is a rule that will
drift.

One consequence worth naming for plan authors: a new persisted field on a registry entry joins the
sync identity comparison automatically, so a plan that adds one owes it a test, not a mechanism.

# Implementation plans

These ship in this order — each depends on the one above it.

| # | plan | what it does |
|---|---|---|
| 1 | [`journal-lookup-keys.md`](journal-lookup-keys.md) | `calls()` filters by `trace_id` and `call_id`; `trace_id` gains an index in every port that can carry one. **Implemented, awaiting review** |
| 2 | [`journal-is-one-row-per-call.md`](journal-is-one-row-per-call.md) | `calls()` returns one row per call with its score; the driver folds the rating in one round-trip; every rating names the call it rates |
| 3 | [`routed-call-identity.md`](routed-call-identity.md) | `stream()` and the tool loop return an object naming the model that answered, instead of bare `str` |

A queued plan is a row in this file and a file beside it. How one is executed lives in `CLAUDE.md`
under "Executing a plan", and the rules binding on every plan live in
[`../reference/invariants.md`](../reference/invariants.md), which is loaded for every task. Neither
is restated here.

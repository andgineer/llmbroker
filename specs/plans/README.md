# Implementation plans

These three ship in this order — each depends on the one above it.

| # | plan | what it does |
|---|---|---|
| 1 | [`journal-lookup-keys.md`](journal-lookup-keys.md) | `calls()` filters by `trace_id` and `call_id`; `trace_id` gains an index in every port that can carry one |
| 2 | [`rating-by-key.md`](rating-by-key.md) | `record_quality` takes the id the host already holds — its own `trace_id`, or a `call_id` — resolving the model at write time; the `(llm_name, operation)` triple is demoted to a primitive |
| 3 | [`routed-call-identity.md`](routed-call-identity.md) | `stream()` and the tool loop return an object naming the model that answered, instead of bare `str` |

A queued plan is a row in this file and a file beside it. How one is executed lives in `CLAUDE.md`
under "Executing a plan", and the rules binding on every plan live in
[`../reference/invariants.md`](../reference/invariants.md), which is loaded for every task. Neither
is restated here.

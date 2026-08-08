# Implementation plans

Order for the plans queued now. A plan that is fully implemented and merged is deleted (anything
spec-worthy moves into `specs/` first) and its row here goes with it.

**How these are executed** — the rules an agent follows when told to implement one — live in
`CLAUDE.md` under "Executing a plan": take the first queued row unless a plan is named, code wins
over a stale plan, gate on `invoke pre` + `pytest` after every batch, never bump the version,
never commit unasked, and leave the plan file in place for review. Nothing needs to be restated in
the request. The plan and its row here are removed only after review and merge, on request.

Statuses as of 2026-08-08: a simplification pass over the implementation. No functionality is
removed by any of them. **Plans 1-7 are implemented and waiting on the maintainer — 1, 6 and 7 are
reviewed, and 6 was reviewed twice: its second round reversed the two-file split it introduced,
for the reasons in its own `Review round 2` section. The next one to take is plan 8.** A row stays
here, with its status, until
the maintainer asks for it to go after merge; "take the first row" means the first row still
marked `queued`.

## Order

| # | Plan | Status | Issue | Blocked by | Notes |
|---|---|---|---|---|---|
| 1 | [http-status-vocabulary](http-status-vocabulary.md) | **implemented, reviewed** | — | — | one module decides what a provider status means; five copies today |
| 2 | [router-failover-and-sse](router-failover-and-sse.md) | **implemented** | — | 1 *(implemented)* | `chat` and `stream` are one algorithm written twice; SSE reader written twice |
| 3 | [learning-as-observer](learning-as-observer.md) | **implemented** | — | — | the store wrapper makes `isinstance` lie; the unmet-budget bound moves to the journal. Schema bump 5 → 6 |
| 4 | [store-retention-dedup](store-retention-dedup.md) | **implemented** | — | — | retention declared in five files, quality record built in two |
| 5 | [lineup-parser](lineup-parser.md) | **implemented** | — | — | **fixes a divergence**: two parsers, different validation |
| 6 | [lineup-file-ownership](lineup-file-ownership.md) | **implemented, reviewed ×2** | — | 5 *(implemented)* | stop assembling the config file as text; then split `upstream.py`. Largest plan. Round 2 reversed the file split: the lineup file has one author |
| 7 | [lineup-refresher](lineup-refresher.md) | **implemented, reviewed** | — | 6 *(implemented)* | written in full against 6's result; **ships with 6** |
| 8 | [models-purity](models-purity.md) | queued | — | 6 *(implemented)* | **skeleton** — `models.py` logs, formats prose, and holds the validators |
| 9 | [direct-client-seam](direct-client-seam.md) | queued | — | 2 *(implemented)* | **skeleton** — the sync broker reaches a private method; two clients copy-pasted |
| 10 | [declared-out-of-catalog](declared-out-of-catalog.md) | queued | — | 6 *(implemented)* | **skeleton** — the `direct=` overlay is half of `Catalog` and owns a cycle back to the broker |
| 11 | [sse-chunk-shape](sse-chunk-shape.md) | queued | — | 2 *(implemented)* | **fixes a spec divergence**: a non-object SSE payload escapes the pool raw instead of failing over. Small |
| 12 | [store-conformance-suite](store-conformance-suite.md) | queued | — | — | tests only, no runtime change: the store layer never got the driver layer's one-suite-for-all shape, so ten universal behaviors are written twice. Take last |

Plans 1-5 touch disjoint files and may be taken in any order subject to the
Blocked-by column. Plans 6 and 7 must reach a release together: 7 extracts the
seam 6 creates. Plan 11 is the only one here that is not a simplification — it
came out of plan 2's review and fixes behavior, so it may be taken out of order.
Plan 12 changes no runtime code at all and blocks nothing, so it goes last: its
own text argues it should be taken only when its purely mechanical diff reads as
worth the churn.

**Skeletons (8-10) carry findings, not routes.** The evidence in them is about
today's code and stays valid; the work order is missing because their target
modules do not exist until the plan that blocks them merges. Each names what its
blocking plan will already have closed — read that section first when writing
the real plan, so work already done is not repeated.

## Standing rules for whatever is queued next

The rules binding on every plan live in [`../reference/invariants.md`](../reference/invariants.md),
which is loaded for every task. Nothing is restated here — a rule written twice is a rule that will
drift.

One consequence worth naming for plan authors: a new persisted field on a registry entry joins the
sync identity comparison automatically, so a plan that adds one owes it a test, not a mechanism.

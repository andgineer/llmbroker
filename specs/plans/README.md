# Implementation plans

Order for the plans queued now. A plan that is fully implemented and merged is deleted (anything
spec-worthy moves into `specs/` first) and its row here goes with it.

**How these are executed** — the rules an agent follows when told to implement one — live in
`CLAUDE.md` under "Executing a plan": take the first queued row unless a plan is named, code wins
over a stale plan, gate on `invoke pre` + `pytest` after every batch, never bump the version,
never commit unasked, and leave the plan file in place for review. Nothing needs to be restated in
the request. The plan and its row here are removed only after review and merge, on request.

Statuses as of 2026-08-03: seven plans queued — a simplification pass over the
implementation. No functionality is removed by any of them.

## Order

| # | Plan | Issue | Blocked by | Notes |
|---|---|---|---|---|
| 1 | [http-status-vocabulary](http-status-vocabulary.md) | — | — | one module decides what a provider status means; five copies today |
| 2 | [router-failover-and-sse](router-failover-and-sse.md) | — | 1 | `chat` and `stream` are one algorithm written twice; SSE reader written twice |
| 3 | [learning-as-observer](learning-as-observer.md) | — | — | the store wrapper makes `isinstance` lie; the unmet-budget bound moves to the journal. Schema bump 5 → 6 |
| 4 | [store-retention-dedup](store-retention-dedup.md) | — | — | retention declared in five files, quality record built in two |
| 5 | [lineup-parser](lineup-parser.md) | — | — | **fixes a divergence**: two parsers, different validation |
| 6 | [lineup-file-ownership](lineup-file-ownership.md) | — | 5 | stop assembling the config file as text; split by owner; then split `upstream.py`. Largest plan |
| 7 | [lineup-refresher](lineup-refresher.md) | — | 6 | **skeleton** — written in full after 6 merges; **ships with 6** |

| 8 | [models-purity](models-purity.md) | — | 6 | **skeleton** — `models.py` logs, formats prose, and holds the validators |
| 9 | [direct-client-seam](direct-client-seam.md) | — | 2 | **skeleton** — the sync broker reaches a private method; two clients copy-pasted |
| 10 | [declared-out-of-catalog](declared-out-of-catalog.md) | — | 6 | **skeleton** — the `direct=` overlay is half of `Catalog` and owns a cycle back to the broker |

Plans 1-5 touch disjoint files and may be taken in any order subject to the
Blocked-by column. Plans 6 and 7 must reach a release together: 7 extracts the
seam 6 creates.

**Skeletons (7-10) carry findings, not routes.** The evidence in them is about
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

# Implementation plans

Order for the plans queued now. A plan that is fully implemented and merged is deleted (anything
spec-worthy moves into `specs/` first) and its row here goes with it.

**How these are executed** — the rules an agent follows when told to implement one — live in
`CLAUDE.md` under "Executing a plan": take the first queued row unless a plan is named, code wins
over a stale plan, gate on `invoke pre` + `pytest` after every batch, never bump the version,
never commit unasked, and leave the plan file in place for review. Nothing needs to be restated in
the request. The plan and its row here are removed only after review and merge, on request.

Statuses as of 2026-08-09: a simplification pass over the implementation, plus one plan that
narrows what the library accepts. Plan 8 was the exception to "no functionality is removed" — it
deleted the hand-named config file as a configuration form, and argued from the mission why that
is a boundary rather than a loss; that claim no longer holds for this batch as a whole.
**Plans 1-8 are implemented and waiting on the maintainer — 1, 6, 7 and 8 are reviewed, and 6 was
reviewed twice: its second round reversed the two-file split it introduced, for the reasons in its
own `Review round 2` section. Plans 9 and 11 are implemented and awaiting review; **the next one to
take is plan 20**, which 11's review turned up. A row stays here, with its status,
until the maintainer asks for it to go after merge.

**The `#` column is identity, not order — read the Take-order line below it.** 16, 17 and 18 were
written after 15 and are taken before it; taking the numerically first `queued` row would start
with a plan whose inventory the later ones delete.

**Take order: 20 → 17 → 19 → 18 → 16 → 10 → 12 → 13 → 14 → 15.** 20 goes first on the same ground
11 did before it: it is the only queued plan that fixes runtime behavior rather than shape, it is
small, and it blocks nothing — "may be pulled forward at any time" is the status in which a plan
stays undone through eight others.

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
| 8 | [curated-source-only](curated-source-only.md) | **implemented** | — | 6, 7 *(implemented)* | **removes a form**, not a simplification: a lineup arrives only as a curated preset name, and no host names a config file. Taken before the skeletons — they would otherwise have tidied code this deletes |
| 9 | [registry-ownership](registry-ownership.md) | **implemented** | — | 8 *(implemented)* | **fixes a trap 8 created**: a sync destroys entries the host put in its own registry. An entry records who wrote it, and a host-built registry object must state what it follows |
| 10 | [model-list-vocabulary](model-list-vocabulary.md) | queued | — | 17, 19, 18, 16 | text only, no runtime change: `lineup` is a coined word that reaches the reader undefined — docs and program strings say "the model list". **Re-inventory the strings** — the four plans before it delete some and write their own new ones in this plan's wording |
| 11 | [sse-chunk-shape](sse-chunk-shape.md) | **implemented** | — | 2 *(implemented)* | **fixes a spec divergence**: a non-object SSE payload escaped the pool raw instead of failing over |
| 12 | [models-purity](models-purity.md) | queued | — | 17, 16 | **skeleton** — `models.py` logs, formats prose, and holds the validators. 17 and 16 both reshape its field set and validator list |
| 13 | [direct-client-seam](direct-client-seam.md) | queued | — | 2 *(implemented)* | **skeleton** — the sync broker reaches a private method; two clients copy-pasted. Unaffected by 16-18 |
| 14 | [declared-out-of-catalog](declared-out-of-catalog.md) | queued | — | 17 | **skeleton** — the `direct=` overlay is half of `Catalog` and owns a cycle back to the broker. **17 closes one of its four findings and raises the rest**: the overlay becomes the only home a named model has |
| 15 | [store-conformance-suite](store-conformance-suite.md) | queued | — | — | tests only, no runtime change: the store layer never got the driver layer's one-suite-for-all shape, so ten universal behaviors are written twice. Take last |
| 16 | [paid-catalog-retirement](paid-catalog-retirement.md) | queued | — | 17 | **closes a gap, not a defect**: an alias whose model the provider withdraws has no representation, so a deployment learns about it from a failing request. After 17, which makes the withdrawal an in-memory fact instead of a stored one |
| 17 | [named-models-are-declared](named-models-are-declared.md) | queued | — | 9 | **removes a form**: a model reached by name is declared in code, never stored. `custom`, `[[custom]]` and `add-model` go; the CLI gains read-only `list`, the alias refresh moves from the merge to the resolution, and `synced` is renamed for the question it answers. **Take first** |
| 18 | [env-one-form](env-one-form.md) | queued | — | — | `env` takes a preset name and drops the form that cannot work on a database registry; plus the undocumented second refresh clock gets its paragraph in `server.md`. Small, independent |
| 19 | [no-automatic-fetch](no-automatic-fetch.md) | queued | — | 17 | **answers a deployment requirement**: a serving process that may make no outbound connection. `sync_interval=None` switches off both clocks, `sync()` with no argument does it explicitly, and `EmptyRegistryError` stops blaming the network for a state the bundled floor makes impossible |
| 20 | [stream-chunk-fields](stream-chunk-fields.md) | queued | — | 11 *(implemented)* | **fixes what 11 left half-closed**: a stream chunk that is an object but whose `choices` are malformed still escapes the pool raw instead of failing over. Small, independent. **Take first** |

Plans 1-5 touch disjoint files and may be taken in any order subject to the
Blocked-by column. Plans 6 and 7 must reach a release together: 7 extracts the
seam 6 creates. Plan 8 goes before the skeletons: it deletes modules and call
sites they would otherwise be written against.

The queued half is ordered so that every plan that *removes* something lands
before the plans that would otherwise be written against what it removes.
**9 fixed a defect and moved the seams** — `models.py`, `merge.py`, the broker
constructor — which is what the skeletons would otherwise have been planned
against. **11 was a behavior fix that blocked nothing and depended on nothing,
and was taken first for exactly that reason** — nothing it touches is reshaped
by anything below, so deferring it only risked it never being taken. **20 is the
same case and goes ahead of the queued half for the same reason**: it finishes
the guard 11 put one level too high, and 13 reshapes the seam between the two
direct clients without touching the SSE helpers it edits.
**10 renames what the reader sees** and now goes after the removals rather than
before them: three plans rewrite the doc sections and CLI strings it was
inventoried from, so renaming first would rename some of them into files that
then delete them. It still goes before 12, which relocates the prose and log
lines out of `models.py`.

**17 goes first of the whole queued half.** It removes a field, a file section
and a CLI command, and moves the alias refresh out of the merge — every other
queued plan is either written against code it deletes (10's string inventory,
12's field set) or has a finding it closes (14's third). 18 follows only because
it edits the same CLI section of `rules/presets.md`; it is otherwise independent.
19 also comes after 17, which rewrites the method whose signature it changes.
16 must come after 17 too, which turns the withdrawal it represents from a stored
fact into an in-memory one. **10 comes after every plan that writes a
user-facing string**, and each of those plans writes its new strings in 10's
wording already — so what 10 renames is what none of them touched, and its
inventory should come out shorter than the one in its text. 15 changes no runtime code and blocks nothing, so it stays
last: its own text argues it should be taken only when its purely mechanical diff
reads as worth the churn.

**Skeletons (12-14) carry findings, not routes.** The evidence in them is about
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

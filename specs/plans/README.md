# Implementation plans

Order for the plans queued now. A plan that is fully implemented and merged is deleted (anything
spec-worthy moves into `specs/` first) and its row here goes with it.

**How these are executed** — the rules an agent follows when told to implement one — live in
`CLAUDE.md` under "Executing a plan": take the first queued row unless a plan is named, code wins
over a stale plan, gate on `invoke pre` + `pytest` after every batch, never bump the version,
never commit unasked, and leave the plan file in place for review. Nothing needs to be restated in
the request. The plan and its row here are removed only after review and merge, on request.

Statuses verified against the code on 2026-07-29: none of the four plans below is implemented.
(`add-model.md` originally described the `add-model` command, which shipped; the file now carries
the model-aliases rework that supersedes it.)

## Order

| # | Plan | Issue | Blocked by | Notes |
|---|---|---|---|---|
| 1 | `mission-conformance-fixes.md` | — | — | Correctness holes (failover surface, `in_flight` slot leak, unbounded `wait`) that bite at pool-limit load; its router fixes are the base for #3's streaming |
| 2 | `sqlite-schema-version-table.md` | #12 | — | Small; kills a live footgun that currently needs a host-side workaround |
| 3 | `add-model.md` (model aliases) | — | #1 | Waiting consumer: echo-words (paid backend + streaming); pool `stream()` builds on #1's transport-error surface |
| 4 | `llm-judge.md` | #8 | — | Largest new feature, no waiting consumer; last |

## Why this order

**#1 first.** Its top findings are correctness bugs, not polish: transport-failure classes that
bypass failover, a leaked in-flight slot that permanently shrinks a model's capacity under
`parallel`, and a `wait` deadline that does not bound the HTTP attempt. llmbroker is a
general-purpose library that may run at the pool's throughput limit, so these rank above new
features. It also rebuilds the router's error surface that #3's pool `stream()` must sit on —
doing #3 first would build streaming failover on the broken surface and redo it.

**#2 is unblocked and independent.** It wanted the typed `SchemaVersionError`, which now exists,
so it can be taken whenever convenient — it shares no files with #1. Its breakage is latent (it
bites on the next schema-version change) and the one affected host carries a workaround, so
nothing degrades while it waits.

**#3 after #1.** The alias design (version-proof `direct()`, catalog-managed custom entries, pool
streaming) has a waiting consumer in echo-words, but its `stream()` depends on #1's transport-error
rework, and its `direct()` restriction must ship in the same release as `stream()`.

**#4 last.** The judge is the largest purely-new feature and nothing external waits for it. It
also inherits one prerequisite from the shipped journal work: the `operation` filter can select a
named operation but not the unlabelled bucket, which stops being harmless as soon as the judge
journals traffic under `llmbroker.judge`. The plan states what must close.

Shared files to mind: #1 and #3 both touch `broker/router.py` and `chat.py` (the reason #3 queues
behind #1) and both touch `cli.py`'s preset fetching (#1 lets `env` take a preset name, #3 makes
`--merge` refresh the catalog).

A rule established by the shipped journal work and binding on #1: **journal reads never
provision** — the journal does not depend on the registry, and a visibility call must survive an
empty or stale one. #1 keeps `calls()` as it is; the rule is recorded in `architecture.md`.

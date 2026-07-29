# Implementation plans

Order for the plans queued now. A plan that is fully implemented and merged is deleted (anything
spec-worthy moves into `specs/` first) and its row here goes with it.

**How these are executed** — the rules an agent follows when told to implement one — live in
`CLAUDE.md` under "Executing a plan": take the first queued row unless a plan is named, code wins
over a stale plan, gate on `invoke pre` + `pytest` after every batch, never bump the version,
never commit unasked, and leave the plan file in place for review. Nothing needs to be restated in
the request. The plan and its row here are removed only after review and merge, on request.

Statuses verified against the code on 2026-07-29: none of the six plans below is implemented.
(`add-model.md` originally described the `add-model` command, which shipped; the file now carries
the model-aliases rework that supersedes it.)

## Order

| # | Plan | Issue | Blocked by | Notes |
|---|---|---|---|---|
| 1 | `typed-exceptions.md` | #11 | — | Smallest, purely additive; supplies the `SchemaVersionError` #4 raises; ship with #2 in one release |
| 2 | `journal-stats-window.md` | — | — | The only plan with a live waiting consumer (dinary's LLM screen); ship with #1 |
| 3 | `mission-conformance-fixes.md` | — | — | Correctness holes (failover surface, `in_flight` slot leak, unbounded `wait`) that bite at pool-limit load; its router fixes are the base for #5's streaming |
| 4 | `sqlite-schema-version-table.md` | #12 | #1 | Small; kills a live footgun that currently needs a host-side workaround; wants #1's typed error |
| 5 | `add-model.md` (model aliases) | — | #3 | Waiting consumer: echo-words (paid backend + streaming); pool `stream()` builds on #3's transport-error surface |
| 6 | `llm-judge.md` | #8 | — | Largest new feature, no waiting consumer; last |

## Why this order

**#1 and #2 first, in one release.** Both are small and both are consumed by the same file in
dinary (`src/dinary/api/controllers/llm.py`): #2 supplies the windowed statistics its LLM screen
needs, #1 lets the same function narrow `except RuntimeError` to `EmptyRegistryError`. Released
together, the host bumps the dependency once and edits that controller once. They touch disjoint
code here (`exceptions.py` + raise sites vs the journal read path), so only the joint release
matters, not the order between them.

**#3 right after.** Its top findings are correctness bugs, not polish: transport-failure classes
that bypass failover, a leaked in-flight slot that permanently shrinks a model's capacity under
`parallel`, and a `wait` deadline that does not bound the HTTP attempt. llmbroker is a
general-purpose library that may run at the pool's throughput limit, so these rank above new
features. It also rebuilds the router's error surface that #5's pool `stream()` must sit on —
doing #5 first would build streaming failover on the broken surface and redo it.

**#4 after #1.** The mismatch it raises becomes `SchemaVersionError` from #1's first commit; its
breakage is latent (bites on the next schema-version change) and the one affected host carries a
workaround, so nothing degrades while it waits.

**#5 after #3.** The alias design (version-proof `direct()`, catalog-managed custom entries,
pool streaming) has a waiting consumer in echo-words, but its `stream()` depends on #3's
transport-error rework, and its `direct()` restriction must ship in the same release as
`stream()`.

**#6 last.** The judge is the largest purely-new feature and nothing external waits for it.

Shared files to mind: #1 and #3 both touch `exceptions.py` (additive in both — merge trivially);
#2 and #3 both touch `broker/broker.py`; #3 and #5 both touch `broker/router.py` and `chat.py`
(the reason #5 queues behind #3) and both touch `cli.py`'s preset fetching (#3 lets `env` take a
preset name, #5 makes `--merge` refresh the catalog).

A second interaction, resolved the same way: #6 journals its judge traffic into the same journal
#2 aggregates, so #2's `calls()`/`stats()` carry an `operation` filter — otherwise a host's
per-model counts would silently include broker-internal calls once #6 lands.

One cross-plan conflict was found during this review and resolved in both files: #3 originally
made `calls()` provision the pool for consistency, while #2 builds `stats()` on the opposite rule.
The rule is now **journal reads never provision** — the journal does not depend on the registry,
and a visibility call must survive an empty or stale one. #3 keeps `calls()` as it is and writes
the rule into architecture.md; #2's `stats()` follows it.

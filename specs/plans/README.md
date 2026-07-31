# Implementation plans

Order for the plans queued now. A plan that is fully implemented and merged is deleted (anything
spec-worthy moves into `specs/` first) and its row here goes with it.

**How these are executed** — the rules an agent follows when told to implement one — live in
`CLAUDE.md` under "Executing a plan": take the first queued row unless a plan is named, code wins
over a stale plan, gate on `invoke pre` + `pytest` after every batch, never bump the version,
never commit unasked, and leave the plan file in place for review. Nothing needs to be restated in
the request. The plan and its row here are removed only after review and merge, on request.

Statuses as of 2026-07-31: neither plan below is started. (`add-model.md` originally described the
`add-model` command, which shipped; the file now carries the model-aliases rework that supersedes
it.)

## Order

| # | Plan | Issue | Blocked by | Notes |
|---|---|---|---|---|
| 1 | `add-model.md` (model aliases) | — | — | Waiting consumer: echo-words (paid backend + streaming); pool `stream()` now has the transport-error surface it needed |
| 2 | `llm-judge.md` | #8 | — | Largest new feature, no waiting consumer; last |

## Why this order

**#1 has the only waiting consumer.** The alias design (version-proof `direct()`, catalog-managed
custom entries, pool streaming) is what echo-words needs for its paid backend. Its one former
blocker is gone: the router's transport-error surface — every failure below the status line cools
and fails over, a malformed 200 included — now exists, so `stream()` can sit on it instead of
re-inventing it. Its `direct()` restriction and `stream()` must still ship in the same release as
each other.

**#2 last.** The judge is the largest purely-new feature and nothing external waits for it. It
also carries one prerequisite of its own: the `operation` filter can select a named operation but
not the unlabelled bucket, which stops being harmless as soon as the judge journals traffic under
`llmbroker.judge`. The plan states what must close.

Shared files to mind: both touch `broker/router.py` and `broker/broker.py`, so they must not run
concurrently.

Two rules established by shipped work and binding on what follows:

- **Journal reads never provision.** The journal does not depend on the registry, and a visibility
  call must survive an empty or stale one. Recorded in `architecture.md`; #2 adds a journal-read
  capability and must not diverge from it.
- **A latency budget is per call, never per model.** `wait` bounds slot acquisition and the
  in-flight attempt; there is no per-LLM timeout knob and will not be one. #1's pool `stream()`
  inherits this — see `architecture.md`.

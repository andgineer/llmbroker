# Implementation plans

Order for the plans queued now. A plan that is fully implemented and merged is deleted (anything
spec-worthy moves into `specs/` first) and its row here goes with it.

**How these are executed** — the rules an agent follows when told to implement one — live in
`CLAUDE.md` under "Executing a plan": take the first queued row unless a plan is named, code wins
over a stale plan, gate on `invoke pre` + `pytest` after every batch, never bump the version,
never commit unasked, and leave the plan file in place for review. Nothing needs to be restated in
the request. The plan and its row here are removed only after review and merge, on request.

Statuses as of 2026-08-01: `preset-sync.md` is implemented and under review; `pool-lifecycle.md`
and `llm-judge.md` are not started.

## Order

| # | Plan | Issue | Blocked by | Notes |
|---|---|---|---|---|
| 1 | `preset-sync.md` | — | — | Closes the zero-admin gap in the mission; changes the `sync` API surface |
| 2 | `pool-lifecycle.md` | — | #1 | Ships in the same release as #1: replaces its removal rule, adds pool-health visibility |
| 3 | `llm-judge.md` | #8 | — | Largest new feature, no waiting consumer |

## Why this order

**#1 closes a mission-level gap** (zero administration currently stops at preset updates) and
changes the public `sync` signature — better landed before new consumers of the API appear. It is
independent of the judge; neither blocks the other.

**#2 must not ship without #1.** It replaces #1's removal rule outright — releasing #1 alone would
publish a rule whose own report makes a promise it cannot keep, and a file writer that can corrupt
a config. Review them as one change.

**#3, the judge,** is the largest purely-new feature and nothing external waits for it. It
carries one prerequisite of its own: the `operation` filter can select a named operation but not
the unlabelled bucket, which stops being harmless as soon as the judge journals traffic under
`llmbroker.judge`. The plan states what must close.

Two rules established by shipped work and binding on what follows:

- **Journal reads never provision.** The journal does not depend on the registry, and a visibility
  call must survive an empty or stale one. Recorded in `architecture.md`; #1 adds a journal-read
  capability and must not diverge from it.
- **A latency budget is per call, never per model.** `wait` bounds slot acquisition and the
  in-flight attempt; there is no per-LLM timeout knob and will not be one — see
  `architecture.md`.

# Implementation plans

Order for the plans queued now. A plan that is fully implemented and merged is deleted (anything
spec-worthy moves into `specs/` first) and its row here goes with it.

**How these are executed** — the rules an agent follows when told to implement one — live in
`CLAUDE.md` under "Executing a plan": take the first queued row unless a plan is named, code wins
over a stale plan, gate on `invoke pre` + `pytest` after every batch, never bump the version,
never commit unasked, and leave the plan file in place for review. Nothing needs to be restated in
the request. The plan and its row here are removed only after review and merge, on request.

Statuses as of 2026-08-02: none of the plans below is started.

## Order

| # | Plan | Issue | Blocked by | Notes |
|---|---|---|---|---|
| 1 | `pool-lifecycle.md` | — | — | Must ship in the same release as the preset-sync change already on main |
| 2 | `preset-autorefresh.md` | — | #1 | Small; finishes the spec position #1 leaves half-stated |
| 3 | `pool-priority.md` | — | #2 | Fixes a live defect: curated priority is discarded by every DB registry |
| 4 | `llm-judge.md` | #8 | — | Largest new feature, no waiting consumer |

## Why this order

**#1 cannot wait for a later release.** The preset-sync change is merged but unreleased, and #1
replaces its removal rule outright: shipping what is on main today would publish a rule whose own
report promises a cleanup it cannot perform, and a file writer that can corrupt a config. Its §4
also carries the defects that the review of that change found and deliberately left unfixed, so
nothing of it survives outside this plan.

**#2 depends on #1 and should not trail it far.** It rests on #1's invariant 4 (which is what makes
an unattended sync safe to apply) and on #1 §1.4 removing the admin-facing WARNING (which is what
lets a refresh run daily without nagging), so it cannot start earlier. It should not start much
later either: #1 §6 patches the `architecture.md` lockfile paragraph with a qualifier, and #2
replaces that paragraph outright. The longer the gap, the longer the spec asserts a position — the
deploy action is the real refresh path — that the next plan inverts.

**#3 must follow #2, and carries a defect fix that is already live.** Curated priority is written
by `sync` and then discarded by every database registry — rows come back sorted by entry name, so
the pool's order is an accident of naming, and `optimizer.md` currently claims otherwise. The fix
replaces the positional index with a weight stored on the entry, which is why it must land after
#2: #2's identity gate decides when a fetched lineup changes nothing, and a new persisted field
changes that comparison. It should not trail #2 far either — once a refresh runs unattended on a
daily clock, installations adopt curated weights with no admin reading them.

**#4, the judge,** is the largest purely-new feature and nothing external waits for it. It
carries one prerequisite of its own: the `operation` filter can select a named operation but not
the unlabelled bucket, which stops being harmless as soon as the judge journals traffic under
`llmbroker.judge`. The plan states what must close.

Two rules established by shipped work and binding on what follows:

- **Journal reads never provision.** The journal does not depend on the registry, and a visibility
  call must survive an empty or stale one. Recorded in `architecture.md`; #1 and #3 each add a
  journal-read capability and must not diverge from it.
- **A latency budget is per call, never per model.** `wait` bounds slot acquisition and the
  in-flight attempt; there is no per-LLM timeout knob and will not be one — see
  `architecture.md`.

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
| 1 | `preset-autorefresh.md` | — | — | Small; replaces the lockfile spec paragraph that currently ships with a qualifier |
| 2 | `pool-priority.md` | — | #1 | Fixes a live defect: curated priority is discarded by every DB registry |
| 3 | `fileless-broker.md` | — | #1, #2 | `Broker()` with no config file; `direct=` for paid models; fixes a second live defect |

## Why this order

**#1 should not wait.** What it rests on is now on main: the invariant that a sync never takes away
a model an installation can call, which is what makes an unattended refresh safe to apply, and the
removal of the admin-facing sync WARNING, which is what lets a refresh run daily without nagging.
What makes it urgent is the other half: `architecture.md`'s lockfile paragraph currently carries a
qualifier — start goes online only when asked, via the `sync=` knob — and #1 replaces that
paragraph outright. The longer the gap, the longer the spec asserts a position, that the deploy
action is the real refresh path, that the next plan inverts.

**#2 must follow #1, and carries a defect fix that is already live.** Curated priority is written
by `sync` and then discarded by every database registry — rows come back sorted by entry name, so
the pool's order is an accident of naming, and `optimizer.md` currently claims otherwise. The fix
replaces the positional index with a weight stored on the entry, which is why it must land after
#1: #1's identity gate decides when a fetched lineup changes nothing, and a new persisted field
changes that comparison. It should not trail #1 far either — once a refresh runs unattended on a
daily clock, installations adopt curated weights with no admin reading them.

**#3 removes the config file from the common path, and carries the second live defect.** A paid
`[[custom]]` alias is re-pointed at the catalog's current model only on the file branch of `sync`,
so every database installation sits on the model id it was first synced with, forever and silently.
That is the same catalog-following machinery `direct=` needs, so it is fixed here rather than
separately. It follows #2 because both touch `LLMConfig` — #2 adds a persisted weight, #3 deletes
`pooled` — and #2's defect is the more expensive one: a wrong routing order affects every request
of every DB installation, while a frozen paid model version affects only installations that declare
one.

Two rules established by shipped work and binding on what follows:

- **Journal reads never provision.** The journal does not depend on the registry, and a visibility
  call must survive an empty or stale one. Recorded in `architecture.md`; the sync's own bounded
  journal read honors it, and what follows must not diverge from it.
- **A latency budget is per call, never per model.** `wait` bounds slot acquisition and the
  in-flight attempt; there is no per-LLM timeout knob and will not be one — see
  `architecture.md`.

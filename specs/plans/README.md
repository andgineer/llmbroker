# Implementation plans

Order for the plans queued now. A plan that is fully implemented and merged is deleted (anything
spec-worthy moves into `specs/` first) and its row here goes with it.

**How these are executed** — the rules an agent follows when told to implement one — live in
`CLAUDE.md` under "Executing a plan": take the first queued row unless a plan is named, code wins
over a stale plan, gate on `invoke pre` + `pytest` after every batch, never bump the version,
never commit unasked, and leave the plan file in place for review. Nothing needs to be restated in
the request. The plan and its row here are removed only after review and merge, on request.

Statuses as of 2026-08-02: neither plan below is started.

## Order

| # | Plan | Issue | Blocked by | Notes |
|---|---|---|---|---|
| 1 | `pool-priority.md` | — | — | Fixes a live defect: curated priority is discarded by every DB registry |
| 2 | `fileless-broker.md` | — | #1 | `Broker()` with no config file; `direct=` for paid models; fixes a second live defect |

## Why this order

**#1 carries a defect fix that is already live.** Curated priority is written by `sync` and then
discarded by every database registry — rows come back sorted by entry name, so the pool's order is
an accident of naming, and `optimizer.md` currently claims otherwise. The fix replaces the
positional index with a weight stored on the entry. It should not wait: a refresh now runs
unattended on a daily clock, so installations adopt curated weights with no admin reading them.

**#2 removes the config file from the common path, and carries the second live defect.** A paid
`[[custom]]` alias is re-pointed at the catalog's current model only on the file branch of `sync`,
so every database installation sits on the model id it was first synced with, forever and silently.
That is the same catalog-following machinery `direct=` needs, so it is fixed here rather than
separately. It follows #1 because both touch `LLMConfig` — #1 adds a persisted weight, #2 deletes
`pooled` — and #1's defect is the more expensive one: a wrong routing order affects every request
of every DB installation, while a frozen paid model version affects only installations that declare
one.

Three rules established by shipped work and binding on what follows:

- **The lineup keeps itself current, and a sync that changes nothing changes nothing.** The refresh
  is unconditional and interval-gated, and an identity gate suppresses every write, application and
  INFO line when the merged result equals what is stored — compared by name for a registry target,
  since a database returns rows in its own order. A new persisted field on `LLMConfig` joins that
  comparison automatically; a plan that adds one owes it a test, not a mechanism. Recorded in
  `architecture.md`, "Keeping the lineup current".

- **Journal reads never provision.** The journal does not depend on the registry, and a visibility
  call must survive an empty or stale one. Recorded in `architecture.md`; the sync's own bounded
  journal read honors it, and what follows must not diverge from it.
- **A latency budget is per call, never per model.** `wait` bounds slot acquisition and the
  in-flight attempt; there is no per-LLM timeout knob and will not be one — see
  `architecture.md`.

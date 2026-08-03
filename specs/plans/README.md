# Implementation plans

Order for the plans queued now. A plan that is fully implemented and merged is deleted (anything
spec-worthy moves into `specs/` first) and its row here goes with it.

**How these are executed** — the rules an agent follows when told to implement one — live in
`CLAUDE.md` under "Executing a plan": take the first queued row unless a plan is named, code wins
over a stale plan, gate on `invoke pre` + `pytest` after every batch, never bump the version,
never commit unasked, and leave the plan file in place for review. Nothing needs to be restated in
the request. The plan and its row here are removed only after review and merge, on request.

Statuses as of 2026-08-03: the queue is empty.

## Order

| # | Plan | Issue | Blocked by | Notes |
|---|---|---|---|---|
| — | — | — | — | nothing queued |

## Standing rules for whatever is queued next

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

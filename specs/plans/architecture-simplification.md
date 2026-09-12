# Plan — simplify routing ownership, deadlines and journal storage

**Status: source-bound against `67659c350`, 2026-09-09.** The maintainer accepted
the architecture analysis; separating ordinary streams from alternative-capable
streams is rejected in
[`streamed-alternatives-live-until-close`](../reference/decisions.md#streamed-alternatives-live-until-close).
This plans the accepted work; implementation has not started.

**Queue position:** first unimplemented plan, bound to the current streamed
continuation and broker-ownership implementation. Finish and review this plan
before revalidating [`load-harness.md`](load-harness.md), then
concretize [`caller-visibility.md`](caller-visibility.md) after that harness.

## Outcome and boundary

One owner manages a streamed call's lifetime; one service settles provider
attempts; one background refresh rebuilds the pool once. A returned answer remains
usable when its journal write fails, but cannot submit a rating through a receipt
whose write was not acknowledged. Positive routed budgets use a fixed deadline.
Database stores update a call's current rating; the zero-dependency file store
keeps its append-and-project implementation.

Preserve `another()`, retained lanes until close, broker-owned cleanup, optional
stream contexts, race widths, recovery parallelism, candidate history, provisional
deltas, replacement exceptions, selection windows and completion-order tie rules.
No retention flag, separate advanced-stream API, first-completion cancellation or
new answer-validation policy is part of this plan. Internal ownership and budget
work must still support all of those existing shapes.

Keep the three public storage ports, optional extras, synchronous facade, direct
client behavior, selection formulas, registry ownership and secret isolation.
No dynamic proxy layer, generic SQL builder, write-behind queue, durable aggregate
store, live-provider experiment, version bump or commit is requested.

## Current evidence and source binding

- `broker/refresher.py`: `_attempt()` calls `sync()`, which rebuilds, and then
  rebuilds again. A live refresher with an unchanged preset reproduced two
  rebuild callbacks for one successful background check.
- `broker/router.py`: `_log_call()` suppresses a failed store write;
  `broker/result.py` nevertheless makes an `AsyncResult` rateable. With a failing
  call write and successful rating write, the in-memory driver held one orphan
  quality record and `calls()` returned no calls.
- `broker/result.py` and `broker/stream_owner.py` both track active operations,
  closure and cleanup tasks. `broker/streaming.py` exposes 21 `StreamBackend`
  fields to connect those modules to private router services.
- The production owner uses `RoutedStream._drain`; the separate `drain_lane`
  helper survives through `Router._drain_lane` for a test. Private type aliases
  in `router.py` also serve importers instead of their defining modules.
- `StreamBudget`, `budgeted_await()` and the owner's pause/resume code implement
  consumer-time exclusion. `backends/driver.py`, the three DB drivers and
  `backends/inmemory.py` each implement the appended-rating projection;
  `standalone/store.py` separately implements the file projection.

Analysis baseline: `invoke pre` passed; `invoke test` reported 1594 passed on the
platform clock and 1106 passed with 488 Docker cases deselected on the coarse
clock. No failures, errors or skips. These are baseline results, not this plan's
implementation gate.

## Decision entries to land with their behavior

These blocks are the complete proposed entries, copied verbatim into
`specs/reference/decisions.md` in the named batch. Reference files describe current
behavior; these proposed changes land only with implementation. Update inbound
links when an anchor changes.

### Batch A — add `a-rating-requires-an-acknowledged-call`

> A result may offer a rating only after the store acknowledged its call record.
> Finishing an answer and acknowledging its record are separate facts. A failed
> write does not discard an answer, cool a model or erase local availability evidence.
>
> **Blocks:** assuming that attempted journaling means successful journaling;
> failing an otherwise usable answer because its journal is unavailable; reading
> the journal before every live-result rating; retry queues for failed records.
> **Why:** a rating without its call cannot be attributed by a later reader.
> The receipt already follows settlement and can carry the acknowledgement without
> another read. A write that raises has an unknown outcome and is conservatively
> unacknowledged. An explicitly non-persistent store may acknowledge acceptance for
> session-only learning; this promises acceptance, not crash-proof durability.

### Batch B — add `one-owner-per-stream-lifetime`

> One owner serializes a stream's operations and closes all work it owns, from
> lazy provisioning through retained answers. A public handle may delegate to it,
> but does not maintain a second independent lifecycle state machine.
>
> **Blocks:** duplicate active-operation and cleanup state in wrapper and worker;
> replacing a callback bundle with an unrestricted reference to the entire broker.
> **Why:** cancellation can happen before a provider attempt exists and while its
> journal is settling. One owner gives those boundaries one authority. Attempt
> settlement is shared by routing modes; choosing the answer remains their own work.

### Batch C — replace `a-budget-is-provider-time-not-wall-clock` with `a-call-has-one-deadline`

> A positive routed budget establishes one monotonic deadline for queueing and
> provider work. Consumer pauses and validation do not extend it. Continuation and
> failover inherit it. Already completed buffered answers remain readable afterwards.
>
> **Blocks:** pausing or extending a deadline at each yielded delta; per-model
> budgets; restarting the budget for another answer; timing out journal settlement
> or resource cleanup merely because provider time has expired.
> **Why:** excluding the reader requires coordinated mutable clocks across lanes
> and handovers. A fixed deadline gives the host a predictable waiting boundary
> with less state. The alternative protects slow readers, but its exactness is not
> required to preserve answers already completed or to avoid blaming a provider.
> **Accepted cost:** a slow reader may exhaust an unfinished ordinary stream.
> Expiry after stream output teaches no latency bound and causes no cooldown or
> failure-streak increment, because elapsed time can include that reader's work.

### Batch C — replace `silence-cools-and-teaches-ordering`

> A caller budget expiring during a provider attempt that has produced nothing
> cools the model and records the budget it missed as ordering evidence. An expiry
> before the provider starts teaches nothing. After streamed output, caller-budget
> expiry teaches neither availability nor latency; the call still records its error.
>
> **Blocks:** blaming every expiry on the caller; cooling an answering stream;
> treating consumer-inclusive time as a provider-latency measurement; dividing
> a call's budget into per-model shares; removing pre-output budget-aware ordering.
> **Why:** silence throughout an attempt is evidence the next caller can use.
> After stream output, a fixed deadline can also expire because the reader held a
> delta. Ignoring that ambiguous sample avoids a second clock or a second signal.
> The ordinary cooldown protects local calls; the journal-derived bound can outlive
> it. Neither changes the distinct role of explicit races or recovery protection.
> **Accepted cost:** stream expiries after output no longer teach a latency bound,
> even when a provider was slow. Unambiguous pre-output misses still do.

### Batch D — replace `a-rating-names-the-call-it-rates`

> A rating names one call. The call supplies its model and operation, and readers
> see one current score on that call. Re-rating replaces that observation rather
> than adding another. A trace lookup resolves one answered call, never a fan-out.
>
> **Blocks:** storing a separate authoritative model and operation on the rating;
> multiplying one opinion into several observations; requiring every backend to
> preserve historical rating events; resurrecting a call by writing its score.
> **Why:** attribution and observation count are the user contract. A database
> can update a score on the existing call atomically; a file journal can append and
> project it. Both answer the same query without requiring database self-joins for
> history the public surface does not expose. Sequential acknowledged writes leave
> the latest value; concurrent writes have backend serialization order, not an
> invented cross-process clock. Lookup windows and retention still bound reads.

### Batch D — replace `learning-from-the-journal`

> Durable evidence for learning lives with call records and their current ratings.
> Live observations update process-local learning; a rebuild replaces derived
> quality windows, budget bounds and metrics from a bounded journal read.
>
> **Blocks:** a second authoritative learning store, persisted aggregate counters
> or per-backend incremental quality calculations.
> **Why:** the call journal already carries the evidence and supports reconstruction.
> Current ratings need no aggregate store, whether updated in place or projected
> from appended file records. Explicitly non-persistent stores support local learning.

### Batch D — replace `a-driver-may-know-the-domain`

> A driver owns the native operations that read calls and replace their current
> scores. The common ports own domain conversion and validation; learning policy
> stays above storage.
>
> **Blocks:** a generic query language to conceal a handful of database differences;
> loading call pages to update one score; implementing ranking inside drivers.
> **Why:** an update by call identity and a filtered page read are ordinary native
> operations. Sharing the domain contract removes repeated policy without forcing
> the file journal and databases into the same physical representation.

## Work order and acceptance

Each batch includes its regression tests and applicable reference edits, then
`. ./activate.sh && invoke pre` and `. ./activate.sh && invoke test`. Complete
all batches before reporting the implementation ready; no intermediate release
is required. Revalidate paths against any intervening changes before editing.

### A. Correct orchestration and record acknowledgement

1. In `broker/refresher.py`, give each explicit or background refresh exactly one
   rebuild site. Background failure/refusal must still attempt a safe rebuild;
   explicit sync still raises its errors. Preserve empty startup, paid-only
   refresh, stamps and scheduling. Do not solve the duplication with a public flag.
2. Make router journal recording report successful acknowledgement internally.
   Carry it through atomic results, stream receipts, replacements and additional
   answers. Preserve completed identity/usage even when recording fails. Keep
   unfinished-result rating as `ValueError`; reject an unacknowledged call with
   `UnknownCallError` before writing a rating or updating the optimizer.
3. Keep `StoreProtocol.record()` and `record_quality()` signatures. A normal return
   acknowledges acceptance, including `InMemoryStore`. A failed rating write does
   not update live quality. Do not retry an ambiguously committed call record.
4. Add regression coverage in `test_rebuild_triggers.py`, `test_rating_by_call.py`
   and `test_answer_recovery.py`: unchanged/changed/refused/failed background sync,
   explicit sync, failed call writes, blocked writes, rating-write failure,
   replacements, additional answers and non-queryable/non-persistent stores.
   Assert one rebuild, no orphan rating through the receipt, retained answer
   identity, and no second slot release or provider penalty for a store failure.

### B. Consolidate lifecycle and attempt settlement without changing routing policy

1. Keep `StreamHandle` as the public type in `broker/result.py`. Place lifecycle
   authority in `RoutedStream` in `broker/stream_owner.py`, created lazily with
   respect to I/O but present before provisioning starts. Move the provisioning
   await under that owner so close/cancel can interrupt it. The public handle
   delegates iteration, continuation and close and retains receipt access.
2. Keep the broker's handle set and shutdown ordering. The owner alone tracks the
   active pull/continuation, closed state and shielded cleanup task. Reject
   concurrent pulls, keep close idempotent, and unregister exactly once. Closing
   an unstarted handle performs no provisioning, acquisition or provider I/O.
3. Extract attempt execution/settlement from `router.py` and `streaming.py` into
   a named internal `broker/attempt.py` service. It owns key resolution for a
   reserved slot, failure classification/application, release and recording.
   Both atomic and streamed attempts use it; keep answer-selection loops separate.
   Route candidates through a narrow typed boundary for acquisition and untried
   selection. Replace `StreamBackend` with these cohesive dependencies, not another
   dictionary of callbacks or an import cycle. Preserve the one shared HTTP client
   and its existing lifetime, including direct clients.
4. Remove the unused `drain_lane` production path and private shim imports in
   `router.py`; update all importers to defining modules. Exercise the real owner
   in the cancelled-before-first-step regression. Leave explicit public facade
   signatures and backend adapters intact.
5. Use `test_answer_recovery.py`, `test_race.py`, `test_router_stream.py`,
   `test_parallel_cap.py` and broker lifecycle tests to preserve active/idle close,
   cancellation during provisioning/provider I/O/journaling, scoped callers,
   immutable receipts, retained alternatives, refill races and completion order.
   Demonstrate no orphan tasks, leaked slots, double records or winner changes.

### C. Replace consumer-adjusted time with a fixed routed deadline

1. Replace `StreamBudget`, its change events and pause/resume machinery with one
   fixed deadline in routed call state. Use ordinary asyncio deadline waits for
   provider I/O and owner waits; check expiry before resuming an unfinished
   consumer-driven lane. Do not introduce a background drain for ordinary visible
   streams. Keep the small attempt-local adjustment excluding yielded consumer
   time from the global provider ceiling; it needs no shared clock or change event.
   Distinguish that ceiling from caller expiry, including with `wait=None` or zero.
2. Positive `wait` starts at routing entry, after lazy provisioning as today;
   it is shared across lanes and `another()`. Keep `None`, zero and negative
   semantics. Settlement remains awaited outside the provider timeout, so this is
   not a promise to return within the budget when a store or cleanup blocks.
3. After deadline expiry, return only complete answers whose provider finished
   within budget. Do not open another candidate or resume an unfinished provider
   read. Preserve initial timeout/error precedence, continuation exhaustion,
   first-delta commitment and replacement behavior. A suspended consumer observes
   expiry on its next pull; retained provider work remains deadline-bounded.
4. Keep pre-output miss learning for both routing paths. Caller expiry after
   streamed output writes an error without a learned budget bound, cooldown or
   streak increment. A global provider-ceiling failure retains its classification.
5. Replace consumer-extension assertions in `test_whole_answer_budget.py` and
   `test_answer_recovery.py`; extend `test_wait_budget.py`, `test_budget_ordering.py`
   and `test_race.py` for a slow reader, validation delays, hidden lanes, multiple
   continuations, buffered completion after expiry, store delay and no new work
   after expiry. Use event gates/controlled clocks and `CLOCK_SLACK` as appropriate.

### D. Store current scores natively in databases

1. Add a narrow `set_score(call_id, score)` operation to `backends/driver.py`.
   SQL drivers update only the existing call's score; Mongo uses an update without
   upsert; the in-memory driver replaces that score. An absent/purged target is a
   no-op and is never created. Public by-id/trace lookup keeps `UnknownCallError`;
   this introduces no new retention-time lookup on the live-result path.
2. Change `DriverStore` to use that operation. Database `journal_view()` becomes
   a filtered call-page read. Remove the quality self-subqueries, Mongo lookup,
   in-memory rating fold and obsolete fold-column guard. Keep call identity,
   timestamps, scope, usage and attempt facts immutable; only the score changes.
3. In `backends/spec.py`, keep one nullable score on call rows and remove the
   separate-event discriminator, rating target and obsolete indexes. Increment
   the internal schema version through the existing schema policy; this is not a
   package version bump. Preserve namespace and mismatch errors. No migration,
   automatic reset, dual schema reader or compatibility shim is added.
4. Keep `FileStore` append-only and its current JSONL format readable, including
   historical rating events. Keep the file-only event shape in its storage policy
   rather than making it the DB contract. Retain non-persistent store behavior.
5. Rework storage tests around public projected calls and current scores:
   `test_driver_conformance.py`, `test_store_backends.py`, `test_store.py`,
   `test_schema_migration.py`, `test_rating_by_call.py` and `test_learning.py`.
   Cover re-rating without increasing observation count, unchanged call time and
   scope, late ratings surviving filters, retention, missing-target no-op,
   preservation of unrelated payload on concurrent score updates, and schema
   mismatch. Ordered writes have a deterministic latest value; concurrent
   unordered writes may leave either score, without losing the call or its fields.

## Reference and documentation placement

- Batch A: replace invariant 1's unconditional write-success promise with the
  acknowledged-call guarantee while keeping its storage rule until D. Update
  `rules/call-path.md`, receipt docstrings and rating guidance. Do not weaken
  answer delivery or slot cleanup to force journal success.
- Batch B: put lifetime ownership in `rules/call-path.md`; keep invariant 19.
  The retained-alternative decision and policy remain intact.
- Batch C: update `rules/call-path.md` and `rules/selection.md` for the fixed
  budget and the narrower latency evidence. Keep invariant 7. Update applicable
  async guides and examples, and all links to the replaced budget decision.
- Batch D: invariant 1 states immutable call facts and attributable current
  ratings without prescribing physical storage. Invariant 8 states that durable
  learning evidence has one source, allowing existing budget bounds and metrics
  and distinguishing configuration/admin verdicts from learned state. Invariant
  11 forbids restoring/sharing live availability, not recording diagnostic facts.
  Align `rules/backends.md`, `rules/selection.md` and `availability-is-not-shared`
  with that distinction; preserve its rejection of a shared availability store.
- Read `mission.md` against the final behavior; edit only changed user
  expectations, not machinery. Remove conflicting restatements and link to the
  single owning rule. Explain the DB schema reset policy in the storage guide.
- Revalidate the harness against fixed deadlines, retained ownership and final
  receipts; leave caller visibility functional until its own queue gate.

## Completion and review handover

Both required gates must pass after each batch and on the final source: activate
first, run `invoke pre`, then `invoke test` on native and coarse clocks with Docker
available. No skipped tests or hidden missing services. Review every fix batch.

Append `## Handover` on implementation completion: batches completed, deviations
and their reasons, exclusions, decisions not already made here, gate counts, and
the remaining queue revalidation. Call out the deliberate budget and DB storage
contract changes separately from behavior-preserving refactoring. This plan remains
until removal is requested.

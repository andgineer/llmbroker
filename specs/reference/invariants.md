# Invariants

Rules whose violation is **silent**: the code compiles, the gate is green, and
the system is wrong. Each is stated here once and nowhere else — the file named
beside it holds the behavior that follows from it.

**Load this file for every task.** An entry earns a place here only when
breaking it is both silent and cross-cutting. A rule local to one subsystem
lives in that subsystem's file instead, where the task itself will lead a reader
to it. The list is capped at ~25: it is loaded on every task, so past the cap an
entry enters only by displacing another.

**Before proposing a mechanism, check it against the scale in
[`mission.md`](mission.md#the-size-of-the-problem).** Coarse is the default
wherever coarse is harmless, and a mechanism justified by a scale this pool
cannot reach is a defect rather than headroom
([`decisions.md`](decisions.md#size-is-part-of-the-mission)). That rule sits
here, outside the numbered list, because it governs how a mechanism is sized
rather than what the running system must never do.

## Where everything lives

| file | what it answers |
|---|---|
| [`rules/call-path.md`](rules/call-path.md) | one routed call — failure classification, `wait`, streaming, the error contract |
| [`rules/selection.md`](rules/selection.md) | which model is picked — cooldown, quality demotion, priority, curated weights, and whether the pool can still fail over |
| [`rules/model-list.md`](rules/model-list.md) | where the models come from, how a curated list becomes the registry, and the four triggers that rebuild the pool |
| [`rules/direct-by-name.md`](rules/direct-by-name.md) | reaching a host's own model — the paid catalog, `direct`, alias contract |
| [`rules/backends.md`](rules/backends.md) | the three ports, source dispatch, lifecycle, DB schema policy, secret naming, and the journal it all writes to |
| [`decisions.md`](decisions.md) | why a contested decision went that way. Open one entry by its anchor — do not read it whole |
| [`mission.md`](mission.md) | what the library is for, and the design that follows from it |
| [`freetier-providers.md`](freetier-providers.md) | curated knowledge about the free endpoints the pool routes over |

## The invariants

1. **The journal is append-only.** No backend ever updates a journal row. A
   quality rating is its own appended record naming the call it rates, and what
   a host reads is a projection over both, never a row rewritten in place. A
   rating is always appended *after* the call it names, so a projection may fold
   the two in one pass; nothing inside llmbroker may offer a host a way to rate a
   call that is not on a row yet. → `backends.md`, `call-path.md`

2. **Nothing inside llmbroker writes the registry but `sync`.** There is no
   add/update/remove path; learning, the optimizer and the admin verbs never
   touch it. What the installation states through the registry port is its own,
   and invariant 22 is what keeps it. → `model-list.md`

3. **The registry stores no ordering.** Every backend returns rows in its own
   order, so an entry's standing must be data on the entry. Nothing may read
   priority out of row position. → `backends.md`, `selection.md`

4. **Nothing a host declares in code enters the routed pool.**
   → `direct-by-name.md`

5. **Nothing but a host rating enters the quality window.** Demotion has no
   time-based recovery, so any auto-generated score — a failure count, an
   outage, a synthetic rating — would demote a model permanently.
   → `selection.md`

6. **Journal reads never provision the pool.** Binding on every journal-read
   API, present and future: a visibility call must keep working on an install
   whose registry is empty, stale, or gone. → `backends.md`

7. **A latency budget belongs to the call, never to the model.** There is no
   per-model timeout knob and will not be one — it could not compose with
   failover. → `call-path.md`

8. **The journal is the only durable state, and quality is the only thing
   derived from it.** There is no second state subsystem holding a truth of its
   own: what a restart must not lose is on a row, never only in memory. Live
   quality is reached two ways and only two — folded forward from the row just
   journaled, which is not optional because a store with no read path has
   nothing else to learn from, and re-derived from the tail, replaced wholesale.
   And a row survives its store whole: a backend that persists part of what it
   was handed is a defect, not a storage choice — the loss surfaces as degraded
   selection, never as an error. → `backends.md`

9. **Every instant crossing the store boundary is UTC, in both directions.** A
   naive value is refused on write and on read rather than guessed at; admitted
   once, it resurfaces as a mis-filed record on every later read.
   → `backends.md`

10. **A client request error never cools a model and never advances its
    streak.** Any 4xx other than 429/401/403 is the request's fault; the model
    is excluded for the rest of that call only. → `call-path.md`

11. **What a process learns about availability stays in that process.** A
    cooldown, a dead key, a within-call exclusion: held in memory, written to no
    store, and never read back from a peer. Sharing it would rebuild the second
    state subsystem invariant 8 forbids and put a read on the call path, to save
    one wasted call that failover already absorbs.
    → `selection.md`, `decisions.md#availability-is-not-shared`

12. **Model identity is immutable.** The same entry name carrying a different
    `model` is an error; a bump must be a new entry name. This protects the
    binding between a name and everything learned about it. → `model-list.md`

13. **The table schema is not a public contract.** Column names and shapes may
    change between releases without notice; the supported read surface is
    `snapshot()` and the store protocols. → `backends.md`

14. **Nothing outside the `llmbroker_` namespace is llmbroker's to write.** Each
    backend keeps its schema marker inside its own prefixed object, so dropping
    the `llmbroker_*` objects fully resets the state. → `backends.md`

15. **A sync never deletes a secret.** A key kept for paid direct calls on the
    same provider is the common case, so deletion could never be the retirement
    mechanism; an orphaned ref is reported and a human decides.
    → `model-list.md`

16. **The registry and everything learned are user-agnostic.** `scope` reaches
    secret refs and journal attribution only: no backend interprets it, and
    nothing — registry, pool, quality — is partitioned per user. → `backends.md`

17. **An empty answer is not an answer.** A well-shaped completion carrying no
    text and no tool calls is the malformed response it is: pooled, the next
    candidate is tried; direct, it raises. Nothing hands a caller an empty result
    as a success. → `call-path.md`

18. **Failover ends at the first delta.** Past it the answer is already partly
    the caller's, and retrying elsewhere could only duplicate or splice.
    → `call-path.md`

19. **The acquired slot is released on every exit path**, including an
    unexpected exception and a cancellation — nothing may permanently shrink a
    model's `parallel` capacity. → `call-path.md`

20. **Every failure state a host must tell apart has its own exception type.**
    A host matching on message text has no contract at all. → `call-path.md`

21. **A key exists only when it is non-empty.** A blank export, an unfilled
    `KEY=` line, a backend returning `""` — all count as unset everywhere. One
    admitted anywhere puts a model with no credential into the pool, where it
    fails every request routed to it. → `model-list.md`

22. **A sync never removes or overwrites an entry it did not write.** Every
    entry records whether a sync put it there, and the default is *no*, so
    anything reaching a registry by any other route is protected without doing
    anything. → `model-list.md`

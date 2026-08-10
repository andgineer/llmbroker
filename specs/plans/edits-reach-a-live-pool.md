# An edit reaches every live process within a minute of activity

The propagation plan `ports-are-the-only-writer` names. Its investigation section
is the evidence; this plan picks the mechanism, records the entries, and writes
back the `server.md` paragraph that plan deleted.

## Goal

**A registry edit, a `disable_llm` and a peer's cooldown reach every serving
process within one debounce window of that process's own activity** — bounded,
without a coordinator, without a background task, and without a single byte of
I/O in a process that is idle.

**One rebuild, no modes.** The re-derivation has no boolean parameters: the work
is one method, the gate is another, and every production call site uses the
gated one.

**Key resolution is not this plan's subject.** What a reconcile asks the secrets
port for, and how long a resolved `api_key_ref` may be reused, is settled by
plan 24 and by the cache plan written against it. This plan changes only when the
rebuild runs.

## What is broken, measured

Reproduced on two brokers over one sqlite database, at the size a pool actually
has — 4 entries over 3 distinct refs, 2000 journal rows, tail limit 300:

- Node A calls `disable_llm("a")`; node B makes **500 successful calls** and
  routes every one of them to `a`. Node A adds an entry; node B's pool after 500
  successful calls is unchanged. One 429 on node B brings both across at once.
  The cause is `Learner.observe`'s `OK` branch (`learning.py:95-96`), which
  re-derives nothing — a process whose calls succeed never re-reads anything.
- One rebuild costs **3.8 ms** (sqlite), **6.3 ms** (postgres), **7.1 ms**
  (mongodb) of database work, of which the tail read is the bulk and does not
  scale with the pool. The `OK` branch joining it costs **+0.006 ms** per call
  over 3000 calls — inside the noise of a 0.82 ms call.
- `force=True` bypasses the debounce entirely, so every rate-limited call already
  pays a full rebuild today: at 50 failing calls a second on postgres, 315 ms of
  database work per second per instance.

**The unit is a live broker instance, not a host.** The documented multi-user
shape builds a broker per request, and its rebuild clock is set by the warm start
the provision performs, so the instance dies inside its own window and this plan
adds no read to it at all. A process holding one broker — the ordinary server —
pays one rebuild per window of its own activity. A host holding a broker per user
pays that many, which is what plan 24 turns back into one.

## Why

Two entries, to land in `decisions.md` verbatim. This plan argues nothing else
about them.

### propagation-rides-the-call-funnel

Another process's edits reach a live pool on the debounced journal rebuild, which
every branch of the call observer enters — the successful one included. The bound
is therefore one debounce window of the process's *own* activity, and a process
that is idle performs no I/O and schedules no wakeups.

**Blocks:** a background poller or refresher task; a listener on the backend's
own change feed (LISTEN/NOTIFY, change streams); a version or epoch marker read
to decide whether the full read is needed; gating the *journal rebuild* before
the call rather than after it.
**Why:** the gate belongs on the funnel public operations already pass through,
because that is what makes work proportional to traffic and costs an idle
deployment nothing. A poller and a change-feed listener both do something while
the process is idle, and a listener needs a connection the library would then
own, on backends where it exists at all — sqlite and the file store have no such
feed. An epoch marker saves the difference between reading the registry and
reading one row, a fraction of a millisecond a minute, and cannot gate the tail
read at all: peers' cooldowns, dead keys and ratings change on every call a peer
makes, not when the registry moves. Gating before the call could fail the request
that carried it here, which is refused
([`lineup-refresh.md`](rules/lineup-refresh.md), "Picking up another process's
edits may never fail the call that carried it here"); the reads sit at the tail
of a call either way, so what a pre-call gate would add is not latency but a way
for another process's edit to break this caller's request.
**Accepted cost:** the observer runs after the call, so the first call of a
process that has been idle past the window is served on the state it already
held, and the refresh lands behind it — the pickup is always the next call, never
this one. And each rebuild is one registry read, one tail read and two reads of
the disabled map, per live broker instance per window of its own activity,
measured together at 3.8 / 6.3 / 7.1 ms on sqlite / postgres / mongodb.

### the-rebuild-has-no-modes

The re-derivation is one method that always does the work and one that runs it
behind the debounce. Nothing chooses a subset, and no caller asks for it out of
turn.

**Blocks:** a `force` parameter that bypasses the window; a parameter that skips
the registry half; any other mode flag on the same path.
**Why:** the immediacy a bypass bought applied only to *other* processes'
evidence — this process's own cooldown and its own dead-key drop are applied in
memory as the failure is classified, before any read. What it cost was an
unbounded rebuild rate: a bypass is not debounced by definition, so a rate-limit
storm paid a full registry and tail read per failing call. Bounding
every path at one window is both the simpler shape and the smaller worst case,
and it is strictly more propagation than today, where a node with no failures
never re-read at all. The mode that skipped the registry saved one read once per
process at start-up.
**Accepted cost:** a peer's cooldown, dead key or registry edit is picked up
within a window rather than at this process's next failure, and the start-up path
reads the registry a second time.

## Work order

Three batches. `. ./activate.sh` first; `invoke pre` and `python -m pytest` green
after each.

1. **One rebuild, no modes** (`broker/learning.py`, `broker/broker.py`).
   - Split `maybe_rebuild` into `rebuild()` — the current body from
     `_safe_resync_registry()` onward, no gate — and `maybe_rebuild()`, which is
     the monotonic comparison, the `_next_rebuild` bump and a call to `rebuild()`.
     Neither takes a parameter.
   - `observe`: the `OK` branch calls `maybe_rebuild()` after `on_success`. The
     `RATE_LIMITED`/`UNAVAILABLE` branch calls `maybe_rebuild()`. The `ERROR`
     branch calls `maybe_rebuild()` and the `shared` local disappears with the
     comment above it — with every path gated, "has nothing to propagate" no
     longer selects anything.
   - `broker.py:256-262` — the warm start calls `rebuild()`. The
     `resync_registry=False` argument and the comment above it go; the extra
     registry read is the accepted cost.
   - The tests that drive the rebuild deterministically (`test_learning.py`,
     `test_pool_priority.py:170`, `test_file_learning.py:90`, `test_broker.py:538`,
     `test_budget_ordering.py:210`, `test_cluster_cooldown.py:51`) call
     `rebuild()`. Grep for `maybe_rebuild(` rather than trusting that list.
2. **The docs paragraph** (`docs/src/en/server.md`, `docs/src/ru/server.md`) —
   below the `sync` paragraph, where plan 22 removed its predecessor. What
   propagates (registry entries, the disabled map, peers' cooldowns and dead
   keys), the bound (one window of the process's own activity, and the first call
   after a long idle is served before the refresh, not after it), that it opens no
   outbound connection and keeps working with the automatic refresh off, and that
   calls in flight are never interrupted. No numbers a knob could contradict.
3. **The specs** (see below), in this batch, not as a sweep.

## Tests

`tests/test_propagation.py`, new — the file this plan exists for:

- `test_a_peer_registry_edit_reaches_a_busy_node` — two brokers on one sqlite
  file; a peer adds an entry; the node's pool holds it after the window, with
  every call successful.
- `test_a_peer_disable_reaches_a_busy_node` — the same shape over `disable_llm`.
- `test_a_peer_cooldown_reaches_a_busy_node` — the case the goal names and no
  existing test covers: a peer's 429 on a shared key withdraws the model on a node
  whose own calls all succeed. Until now a peer's cooldown arrived only through
  the forced path, which only a failure of one's own could open.
- `test_an_idle_broker_reads_nothing` — a counting store and registry, the
  automatic refresh off so the second clock cannot answer for the first; no
  calls, no reads, across a period longer than the window.
- `test_the_rebuild_is_debounced` — N successful calls inside one window produce
  exactly one registry read.
- `test_the_first_call_after_the_window_is_served_before_the_refresh` — the
  accepted cost, asserted rather than left to a reader.

`tests/test_learning.py` — `rebuild()` and `maybe_rebuild()` each have a test:
the first always works, the second is a no-op inside the window. The existing
`test_maybe_rebuild_skips_resync_registry_when_disabled` goes with the parameter.

## Spec updates

- `decisions.md` — the two entries above, verbatim.
- `rules/journal.md`, "One tail read derives everything" — the sentence about the
  read being forced out of turn goes; what replaces it is that every observed
  call enters the same gate, so the bound is one window of the live instance's
  own activity. Link the first entry.
- No new invariant, and none of the 22 changes: the bound is local to how a live
  pool re-reads shared state, and `rules/journal.md` is where a task about it
  lands.

## The queue

Independent of everything queued. It ships after `ports-are-the-only-writer`,
whose deleted `server.md` paragraph batch 2 replaces — releasing this plan
without it would publish two descriptions of the same clock.

It writes user-facing strings only in the docs, in plan 10's wording already, so
it does not lengthen that plan's inventory.

**Everything about keys is left to plan 24 and the cache plan behind it.** What a
reconcile resolves for, per entry or per distinct ref; how long a resolved ref is
reused; and whether a key that only another process can see reaches this one — all
of it depends on the key leaving the pool slot, which is 24's work. Taking this
plan first keeps that separation: it changes when a rebuild runs and nothing about
what a rebuild pays for a key.

## Gate

`invoke pre` clean and `python -m pytest` green.

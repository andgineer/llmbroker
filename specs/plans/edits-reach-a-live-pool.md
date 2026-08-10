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

**A periodic pass costs nothing where a lookup is free, and a metered backend
decides for itself.** Making the reconcile periodic turns key resolution into
periodic traffic; where that traffic is billed and slow, the backend that bills
it holds its own answer for a bounded time.

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
  database work per second per node.

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
to decide whether the full read is needed; gating the re-read *before* the call
rather than after it.
**Why:** the gate belongs on the funnel public operations already pass through,
because that is what makes work proportional to traffic and costs an idle
deployment nothing. A poller and a change-feed listener both do something while
the process is idle, and a listener needs a connection the library would then
own, on backends where it exists at all — sqlite and the file store have no such
feed. An epoch marker saves the difference between reading the registry and
reading one row, a fraction of a millisecond a minute, and cannot gate the tail
read at all: peers' cooldowns, dead keys and ratings change on every call a peer
makes, not when the registry moves. Gating before the call would put a database
read and a secrets read in the request's latency path and could fail the
request, which is refused ([`lineup-refresh.md`](rules/lineup-refresh.md),
"Picking up another process's edits may never fail the call that carried it
here").
**Accepted cost:** the observer runs after the call, so the first call of a
process that has been idle past the window is served on the state it already
held, and the refresh lands behind it — the pickup is always the next call, never
this one. And each rebuild is one registry read plus one tail read per node per
window of activity, measured at 3.8 / 6.3 / 7.1 ms on sqlite / postgres /
mongodb.

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
storm paid a full registry, secrets and tail read per failing call. Bounding
every path at one window is both the simpler shape and the smaller worst case,
and it is strictly more propagation than today, where a node with no failures
never re-read at all. The mode that skipped the registry saved one read once per
process at start-up.
**Accepted cost:** a peer's cooldown, dead key or registry edit is picked up
within a window rather than at this process's next failure, and the start-up path
reads the registry a second time.

### secret-staleness-belongs-to-the-backend

How long a resolved `api_key_ref` may be reused is decided by the secrets backend
that answers, not by the caller that asks. The backends whose lookup is a local
read answer every time; the ones whose lookup is a billed network round trip hold
their answer for a bounded time, present or absent alike.

**Blocks:** an age or TTL on the resolution held by the pool's own reconcile; a
capability on the secrets protocol by which a backend declares itself expensive;
a knob on the broker for how stale a key may be; caching a resolved key while
re-asking for a missing one.
**Why:** the cost that motivates any staleness at all is a property of the store,
not of the key — an environment lookup and a database row are the same order as
the reads the pass already does, and holding those for an hour would buy nothing
and lose accuracy. A caller cannot know the cost: a host may implement the
protocol over anything, so a rule written in the core is a guess that is wrong
for every backend it did not anticipate, and a protocol bit only moves the same
guess one level out while leaving the machinery in the core. Deciding it where
the round trip is actually made is also what the vendors themselves do. The
asymmetry between a hit and a miss fails the same test: a pool with a provider
its installation never keys is the ordinary steady state, so re-asking for what
is absent is the case that would poll forever.
**Accepted cost:** a key rotated or removed in a metered store keeps routing for
up to that backend's own window, and there is no operation that clears it — an
installation that needs immediacy restarts the process, which its deploy already
does.

## Work order

Four batches. `. ./activate.sh` first; `invoke pre` and `python -m pytest` green
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
2. **One cache, used by the two backends that need it** (`secrets.py`, `aws/`,
   `vault/`).
   - The cache is written **once**, beside the secrets protocol, as a small
     wrapper a backend composes into its own `resolve`: a ref → (value or
     absence, taken at) map with an expiry the composer supplies. It is not a
     base class and not a mixin — `aws.Secrets` and `vault.Secrets` each hold one
     and parameterize it; every other backend never touches it and keeps
     resolving on every ask. Two backends caching by hand is the copy-paste this
     batch exists to prevent.
   - Both backends take the window as a constructor argument, defaulting to an
     hour — the same default the vendor's own caching client ships with. No knob
     reaches the broker.
   - Absence is cached like a value, per the entry above.
   - `standalone`, the DB-backed secrets and any host implementation are
     untouched: they answer every ask, so their staleness is one pass.
3. **The docs paragraph** (`docs/src/en/server.md`, `docs/src/ru/server.md`) —
   below the `sync` paragraph, where plan 22 removed its predecessor. What
   propagates (registry entries, the disabled map, peers' cooldowns and dead
   keys), the bound (one window of the process's own activity, and the first call
   after a long idle is served before the refresh, not after it), that it opens no
   outbound connection and keeps working with the automatic refresh off, and that
   calls in flight are never interrupted. No numbers a knob could contradict.
   Say in the same place that a key read from a metered store is held for a
   bounded time, so a rotation there is not instant.
4. **The specs** (see below), in this batch, not as a sweep.

## Tests

`tests/test_propagation.py`, new — the file this plan exists for:

- `test_a_peer_registry_edit_reaches_a_busy_node` — two brokers on one sqlite
  file; a peer adds an entry; the node's pool holds it after the window, with
  every call successful.
- `test_a_peer_disable_reaches_a_busy_node` — the same shape over `disable_llm`.
- `test_an_idle_broker_reads_nothing` — a counting store and registry; no calls,
  no reads, across a period longer than the window.
- `test_the_rebuild_is_debounced` — N successful calls inside one window produce
  exactly one registry read.
- `test_the_first_call_after_the_window_is_served_before_the_refresh` — the
  accepted cost, asserted rather than left to a reader.

The cache, over a counting fake and then over the real backends:

- The wrapper's own tests, beside the secrets protocol — a repeat ask inside the
  window does not reach the resolver, one past it does, and an absent ref behaves
  identically to a present one.
- `tests/test_secrets.py` — `aws.Secrets` and `vault.Secrets` on their existing
  LocalStack and Vault containers: two resolutions of the same ref make one call
  to the service; a value changed in the service behind a short window is picked
  up once it expires.
- The same file — `standalone` and the DB-backed secrets resolve on every ask,
  so nothing was cached where a lookup is free.

`tests/test_learning.py` — `rebuild()` and `maybe_rebuild()` each have a test:
the first always works, the second is a no-op inside the window. The existing
`test_maybe_rebuild_skips_resync_registry_when_disabled` goes with the parameter.

## Spec updates

- `decisions.md` — the three entries above, verbatim.
- `rules/journal.md`, "One tail read derives everything" — the sentence about the
  read being forced out of turn goes; what replaces it is that every observed
  call enters the same gate, so the bound is one window of the process's own
  activity. Link the first entry.
- `rules/backends.md`, in the secrets part — one sentence that how long a
  resolved ref may be reused is the answering backend's decision, and that the
  shipped metered ones hold it for a bounded time while the local ones answer
  every ask. Link the third entry.
- `rules/pool-health.md`, "The measure is key presence, and it never lags behind
  the keys" — the rule survives for every local backend and is now bounded, not
  absolute, for a metered one. One clause, not a rewrite.
- No new invariant, and none of the 22 changes: the bound is local to how a live
  pool re-reads shared state, and `rules/journal.md` is where a task about it
  lands.

## The queue

Independent of everything queued. It ships after `ports-are-the-only-writer`,
whose deleted `server.md` paragraph batch 3 replaces — releasing this plan
without it would publish two descriptions of the same clock.

It writes user-facing strings only in the docs, in plan 10's wording already, so
it does not lengthen that plan's inventory.

**Which entries a reconcile resolves for is not this plan's question.** It
resolves per entry rather than per distinct ref, and for the whole pool rather
than for the model a call chose — at the size a pool has, a handful of lookups a
window, and after batch 2 they are local reads or cached ones. Whether the
routing path should ask at all is a question about what pool health means, and it
is settled on its own, not here.

## Gate

`invoke pre` clean and `python -m pytest` green.

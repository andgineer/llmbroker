# The host never writes llmbroker's tables

Two things travel together because they are the same mistake seen twice. The
specs sanction a host writing rows into `llmbroker_*` by hand, which contradicts
invariant 13 in the same breath. And the paragraph plan 18 added to `server.md`
describes how such a change reaches a running process — a mechanism that does
not exist as described. The rule is fixed here; the mechanism the second half
points at is investigated here and fixed by the plan that follows.

## Goal

**One writer: llmbroker's own ports.** A host that needs pool members of its own
states them through the registry port — a driver it implements, or a registry
object it hands over. It does not run `INSERT` against a table whose column
names invariant 13 explicitly refuses to promise. That llmbroker's own ports
write those tables is not an exception to the rule: the ports are llmbroker,
working by llmbroker's rules.

**Nothing is published about a mechanism that is about to change.** The
`server.md` paragraph goes rather than being reworded. The true paragraph is
written by the propagation plan, against the behavior that plan produces.

## Why

One recorded entry is touched, and only its illustrations.

`decisions.md#a-sync-touches-only-what-a-sync-wrote` names "a hand-written row"
and "a database it filled by hand" among the routes an entry may legitimately
reach the registry by. **Its reasoning survives whole** — ownership is a property
of the entry, not of the backend class or of the constructor call, and that is
what makes the mixed pool statable. What dies is the two illustrations, because
they invite a host to write a table llmbroker does not promise the shape of, and
because the same entry's `Blocks:` line already refuses to infer ownership from
the construction path. The entry keeps its decision and loses two examples.

The amended block, to land verbatim in `decisions.md`:

> ### a-sync-touches-only-what-a-sync-wrote
>
> Every registry entry records whether a sync put it there. A merge partitions on
> that record: entries a sync wrote are the ones the removal rule may retire and
> the arriving lineup may replace; an entry the installation stated itself is
> carried through untouched, whatever the arriving lineup says. The default for a
> new entry is *not written by a sync*, so anything reaching a registry by any
> other route — a driver the host implements, a registry object it hands over, a
> host's own mirror call — is protected without doing anything.
>
> **Blocks:** deciding what a merge may remove from where the registry came from,
> or from whether the broker was constructed with an object; inferring ownership
> from the entry's shape; a per-registry "read-only" flag; a host write path into
> the shipped backends' tables.
> **Why:** ownership is a property of the entry, not of the backend class or the
> constructor call. A host may implement a driver of its own that holds our
> curated pool, and may equally hand over a registry object holding entries it
> states itself — the construction path predicts nothing. It also makes the mixed
> pool statable: the routed pool is whatever the registry states as pool members,
> whether it came from the curation, from the host, or from both. The host's side
> of that is always the port, never our table: invariant 13 promises nothing about
> a column name, so a hand-written row is a deploy script that breaks on an
> upgrade with no error to read.
> **Accepted cost:** one more fact stored per entry. (unchanged — keep the
> existing remainder of this paragraph verbatim)

## What this narrows, stated plainly

A host can no longer put an entry into a **shipped** backend by hand, and
llmbroker offers no add/update/remove API to replace that (invariant 2, and plan
17 removed the last CLI verb that looked like one). The routes that remain are
the port ones: implement `RegistryProtocol` over storage of your own, or wrap a
shipped registry in a small composing implementation whose `load()` returns the
shipped rows plus the host's own entries.

That composing wrapper is the mixed pool's route, and it needs nothing new from
llmbroker: on the next merge the host's entries arrive through `load()`, are
carried through untouched by the partition, and are persisted by `Catalog.apply`
like everything else the merge holds — marked, correctly, as not written by a
sync. **Verify this before writing the spec text** (`refresher.py:250-255`,
`_registry_target` → `apply(merged)`): if the composing route does not in fact
work end to end, the narrowing costs the mixed pool, and that is a product
decision to bring back rather than absorb.

## Work order

Two batches, both text.

1. **The rule.**
   - `decisions.md` — the amended entry above.
   - `invariants.md` #2 — "What the installation puts there by its own hand is
     its own" becomes a clause that names the port, not a hand. One clause, no
     new entry: the cap stands.
   - `rules/sync-merge.md:56-60` — the partition paragraph drops "a hand-written
     row" and "a backend filled before llmbroker ever ran" and states the port
     routes instead. Add one sentence: the host's write surface is the port, the
     table is llmbroker's own, per invariant 13.
   - `rules/backends.md`, beside "A string moves the storage; an object moves the
     ownership" — one sentence that a connection string never invites a host
     write.
2. **The texts written on the old assumption.**
   - `docs/src/en/server.md` and `docs/src/ru/server.md` — delete the
     second-clock paragraph plan 18 added. Nothing replaces it in this plan.
   - Same files, the `sync` paragraph below it: "rows you put in the registry
     yourself are left alone" / "строки, которые вы внесли в реестр сами" — say
     entries the installation states through its own registry, not rows a person
     put in a table.
   - Grep both language trees for any other place that shows a host editing the
     registry directly; the two above are the ones known today.

## The investigation this plan carries

The propagation plan is written against this section, not against a guess. What
is already established, so it is not re-derived:

- `Catalog.resync()` has exactly two callers: the debounced journal rebuild
  (`broker.py:171`) and `refresher.sync()` under `if changed and self._live()`
  (`refresher.py:220`). There is no third path by which a live pool re-reads the
  registry.
- The journal rebuild fires only on failure. `Learner.observe`'s `OK` branch
  (`learning.py:95-96`) does not call `maybe_rebuild`, so a process whose calls
  all succeed never re-reads the registry. Verified: 10 000 successful calls
  produce zero re-reads, one rate-limited call produces one.
- `changed` is computed against what is **stored**, not against what this
  process holds in memory (`refresher.py:250-252`). So when the deploy job or a
  peer has already merged the new preset, every other node's own check finds
  `changed=False` and skips the resync. Exactly one node in a fleet — whichever
  merged first — updates its pool promptly; the rest wait for a failure.
- `disable_llm`/`enable_llm` (`broker.py:459-473`) write the store's disabled map
  and update the calling process at once. Peers pick it up through
  `_resync_disabled`, which is inside the same failure-driven rebuild.

**The question:** what makes a registry or disabled-map change written by any
process reach every other live process within a bounded time, without a
coordinator and without llmbroker running a service of its own (mission,
positioning 1 and requirement 6).

**What the answer must satisfy.** An idle process performs no I/O and schedules
no wakeups — the time gate stays on the funnel that public operations already
pass through, never on a timer of its own. Picking up a peer's edit may never
fail the caller's request (`lineup-refresh.md`, "Picking up another process's
edits may never fail the call that carried it here"). And the cost must be
stated at the pool's throughput limit, not at a comfortable one.

**What to weigh, and against what.** The staleness side is not symmetric with
the query side: a process holding a withdrawn model spends a real call and a
failover on it, and a process that has not seen a demotion or a `disable_llm`
keeps routing to a model the host has already judged. The query side is one
read per node per interval, independent of throughput. Measure both rather than
asserting either — including what the read costs on each shipped backend at the
tail-read limit the rebuild already pays.

**Candidates, with what is already recorded against them.**

- *The debounce becomes a floor as well as a ceiling* — the `OK` branch joins
  the unforced `maybe_rebuild`, so the existing 60-second gate fires on activity
  rather than only on failure. Nothing new is stored, no protocol method is
  added, and the interval already exists. Costs one registry read plus one tail
  read per node per minute of activity, on a path that already writes a journal
  row per call.
- *A version or epoch marker the nodes read cheaply.* Re-examine, do not assume:
  the recorded rejection of a conditional GET (`lineup-refresh.md`, the identity
  gate — "would save a kilobyte and no round trip while proving strictly less")
  was written about a network fetch. Whether it transfers to a local database
  read is the thing to decide, and the answer belongs in a new `decisions.md`
  entry either way, since the alternative deserves a search key.
- *Backend-native push* — LISTEN/NOTIFY, change streams. Weigh against the
  backends that have nothing of the sort (sqlite, the file store) and against
  the standing rule that the library owns no running service.

The plan that follows this one picks one, records the entry, and writes the
`server.md` paragraph this plan deletes.

## Tests

No new tests: both batches are text. The gate is the existing suite staying
green — the specs and docs carry no executable content, and the doctests that do
run live in `src/` and are untouched.

## Spec updates

Named in the work order: `decisions.md`, `invariants.md` #2,
`rules/sync-merge.md`, `rules/backends.md`. No new invariant — the rule is local
to how a registry is written and lives with the partition it qualifies.

## The queue

**Ships with plan 18.** It deletes a paragraph 18 added, so releasing 18 alone
publishes a promise this plan withdraws. The README row says so, as 21 does for
19.

Independent of everything else queued. It writes no strings a reader sees, so it
does not add to plan 10's inventory; it deletes two paragraphs from 10's
inventory instead.

## Gate

`invoke pre` clean and `python -m pytest` green.

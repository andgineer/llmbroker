# One switch for "this process does not fetch", and one call that does it explicitly

## Goal

An installation may be required to make no outbound connection while it serves a
request, except to the providers themselves — an egress policy, an audit rule, a
network a serving process is not on. llmbroker's refresh does not cost such a
process any latency: the task is created and never awaited, and the blocking
fetch runs off the event loop in a thread. But it is still an outbound connection
the operator did not schedule, and there is today no way to say no to it.

Half the answer exists: `sync=None` plus an explicit `await llms.sync("freetier")`
in a deploy job, which `server.md` already recommends for filling a database. Two
holes remain, and both are in the paid catalog:

1. `sync=None` still arms a clock when anything in `direct=` names an alias, so
   the installation this is most likely to describe — its own registry plus one
   paid model by name — keeps fetching on request traffic.
2. There is no explicit way to refresh the catalog. `sync` takes a preset name,
   and an installation that follows no preset cannot ask for the one thing it
   does follow.

After this plan a deployment states once that nothing is fetched automatically,
and does the fetching where it already does its migrations, with one call it can
await.

## Why

The one `decisions.md` entry below carries it. Nothing else in this plan argues.
The default does not change: what this adds is a way out of it, and the way out
is documented with what it costs.

## The entry, to land in `decisions.md` verbatim

### no-automatic-fetch-means-none-at-start-either

Turning off the automatic refresh turns off the fetch that fills an empty
registry at the first provision. An installation that has said it does its own
fetching gets an error naming that, not a fetch it forbade.

**Blocks:** keeping the start-fill alive as "just once per process"; a second
switch to control it separately; falling back to the bundled preset to fill a
registry the operator meant to fill itself.
**Why:** "once per process" is not a bound an operator can hold — a serving fleet
restarts, scales out and cycles pods, so a fetch that happens once per process is
a fetch that happens on request traffic, which is exactly what the switch was
thrown to prevent. The failure it produces instead is the good one: it happens at
the first call, names the missing deploy step, and cannot be mistaken for a
network problem. A second switch would let the two be set inconsistently, and
there is no installation that wants a fetch at start but not later — the reason
to forbid one forbids both.
**Accepted cost:** an operator who sets the switch and forgets the deploy step
gets a broker that will not serve. That is the intended outcome; the alternative
is a broker that serves and quietly fetched to do it.

## Work order

Three batches. Each ends green.

### 1. The switch

1. **`broker/broker.py`.** `sync_interval` takes `float | None`; `None` means no
   automatic refresh of anything — neither the curated preset nor the paid
   catalog. `_check_broker_args` accepts it. Zero is not the value: `_arm` reads
   zero as a deadline already passed, so it would mean *refresh on every request*,
   the opposite.
2. **`broker/refresher.py`.** `interval` becomes `float | None`. `schedule()`
   returns on the first line when it is `None`. `_arm` is not called. In
   `before_provision`, the empty-registry branch does not fetch — it returns, and
   `Catalog.provision` raises with the message from batch 3.
3. The explicit `sync()` is unaffected by the switch. It is the caller's own act,
   which is the whole point.

### 2. The one call

4. **`broker/refresher.py`, `broker/broker.py`, `sync.py`.** `sync` takes
   `source: str | None = None`. With no argument it does what this installation
   follows: the preset it was constructed with, or — where it follows none — the
   paid catalog, which is the only thing a declared alias rides on. An explicit
   name still overrides, as today.
5. The catalog-only path returns `None`, so the signature becomes
   `SyncReport | None`. There was no merge, and returning an empty report would
   put a line in a deploy log claiming a sync ran that did not.
6. An installation that follows no preset *and* declares no alias gets `None` and
   no fetch — there is nothing it follows.

### 3. The error says what actually happened

7. **`exceptions.py` / `broker/catalog.py`.** `EmptyRegistryError`'s message
   names the two causes that are real. Today it says the lineup "could not be
   fetched" and to check network access — but a fetch has the cached copy and the
   wheel's copy behind it, so a network failure alone cannot produce this state.
   What can: the broker was told to follow nothing and nothing filled the
   registry, or the fill was attempted and the registry write failed, which
   `_attempt` logs and swallows by design. With the switch on, the message names
   the deploy step.

**Every user-facing string this plan writes — the message above, the docs below —
says "the model list", never "lineup".** `model-list-vocabulary` removes the
coined word from everything a reader sees, and it is taken after this plan;
writing the word now would only add a line to its inventory.

## Tests

- With `sync_interval=None` and a populated registry: a call provisions, serves,
  and the preset source is never read — assert against the source, not by timing.
- With `sync_interval=None` and an empty registry: the call raises
  `EmptyRegistryError`, and nothing was fetched.
- With `sync_interval=None`, `direct=["opus"]` and a warm catalog cache: a call
  resolves the alias and fetches nothing.
- The switch does not disable the explicit call: `await llms.sync("freetier")`
  fetches and merges with it set.
- `sync()` with no argument on a broker following a preset merges that preset and
  returns a report.
- `sync()` with no argument on a broker with `sync=None` and a declared alias
  refreshes the catalog, returns `None`, and writes nothing to the registry.
- `sync()` with no argument on a broker with `sync=None` and no declared alias
  returns `None` and fetches nothing.
- The default is unchanged: with no `sync_interval`, the clock arms and a call
  past the interval schedules a refresh.
- `EmptyRegistryError`'s message names the deploy step and does not mention
  network access.

## Spec updates

- **`rules/lineup-refresh.md`** — the two gates gain the case where there is no
  clock at all, and the rule that the explicit call is never gated by it.
- **`decisions.md`** — the entry above, verbatim.
- **`mission.md`** — verify only. Requirement 2 says the curated lineup keeps
  itself current inside a running installation, which stays the default and stays
  true. An opt-out does not change what the library is for, and naming it in the
  mission would be describing a knob.

## Docs (en and ru, in step)

- **`server.md`, its own short section**: the deployment that may not fetch while
  serving. The two lines — the awaited call beside the migrations, and the
  constructor switch — and then what it costs, stated as plainly as the mission
  states it: free endpoints are retired without notice, so a list nobody refreshes
  decays, and the freshness is now the operator's job. This is the section a
  reader arrives at from an egress rule, so it says what to run and how often.
- **`usage.md`** — the `sync_interval` example already there gains the `None`
  value with one line of what it means.

## The queue

After 17, which rewrites what `Refresher.sync` does internally; taken before it,
that plan would re-edit the same method. Nothing is blocked in the meantime — the
pool half of this is already reachable today with `sync=None` plus an explicit
`sync("freetier")`, and it is only the paid catalog that has no answer until this
lands.

## Gate

`invoke pre` clean and `python -m pytest` green after each batch. Docker up for
the testcontainer tests.

## Handover

**Done, in the plan's three batches.** All of the work order, all nine tests, the
spec updates and the docs in both languages.

- **Batch 1** — `sync_interval` is `float | None` on both façades; `None` returns
  from `schedule()` on its first line, arms nothing in `before_provision`, and
  does not fill an empty registry there. `_arm` and the `_attempt` deadline reset
  both narrow the interval locally, since the guard at the call site does not
  reach into a separate method.
- **Batch 2** — `sync(source=None)` on the refresher and both façades, returning
  `SyncReport | None`. With no argument and no preset followed it refreshes the
  paid catalog and returns `None`; with no alias declared, `_refresh_paid_catalog`
  already returns on its first line, so that installation fetches nothing.
- **Batch 3** — two `EmptyRegistryError` messages in `catalog.py`, picked by a
  constructor flag the broker sets from `sync_interval is not None`. Neither
  mentions the network.

**Decisions the plan did not make.**

- *The explicit catalog-only sync raises rather than swallowing a fetch failure.*
  A preset sync warns and continues when the catalog is unreachable, because the
  model list must still merge; a catalog-only sync has nothing else to do, and a
  deploy job that printed nothing and exited zero would report a refresh that did
  not happen.
- *The catalog-only sync writes its check record*, like any other completed check.
- *`Catalog` learns one fact about the installation* (whether anything may fill an
  empty registry) rather than the broker composing the message: the text stays
  where it is raised.

**Deviations.**

- *`decisions.md#unconditional-lineup-refresh` was amended*, which the plan does
  not mention. It recorded "**Blocks:** an off switch", and this plan builds one —
  leaving it would have left the file contradicting the code. What it blocks is
  now stated as what it always argued: freezing the list while the process keeps
  serving from it. The new entry is cross-linked from it.
- *`mission.md` was edited, not only verified.* Its headline claim was "The lineup
  keeps itself current, **unconditionally**", which is now false as written: a
  reader who skips `rules/` would conclude an egress-restricted deployment is
  impossible. The paragraph now says the list is never frozen and that a
  deployment forbidden to fetch while serving takes the job over rather than
  dropping it — intent, with no knob named. Requirement 2 was left alone; it
  describes the default, which does not change.

**Raised, not implemented — one hole the plan's scope leaves open.** With
`sync_interval=None`, `direct=["opus"]` and a *cold* catalog cache, the first
alias resolution still reads the paid catalog through the fetch chain, so a fresh
pod goes to the network once — the plan's own test states the guarantee for a warm
cache only. The deploy job cannot warm that cache for a serving pod: it runs in a
different container. Closing it means an offline read mode on the preset source
(cache, then the wheel's copy, never the network) selected when the switch is on,
which resolves from the frozen bundled copy instead — the same floor a first
offline run already uses. That is a new parameter through two call layers and was
not in the work order, so it is not in this diff. **Recommendation: do it, as a
small follow-up plan** — the deployment this plan is written for is exactly the
one that hits it.

**Gate:** `invoke pre` — all checks passed. `python -m pytest` — 1283 passed, zero
skips, zero errors (Docker up; the testcontainer tests ran).

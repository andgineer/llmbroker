# One broker per process, and a pool rebuilt on four triggers

The deletion pass. One plan, six batches, each independently green. It brings the
implementation down to the scale
[`mission.md`](../reference/mission.md#the-size-of-the-problem) now states, and it
is what makes the anchors already written into `invariants.md` and `decisions.md`
true.

## Goal

**A broker is the installation and lives as long as the process.** It holds the
ports, the pool, the quality it has learned, the HTTP client. A request holds a
caller — the scope it writes on its journal rows and the keys it may pay with —
and creating one costs no I/O.

**The pool is rebuilt on four triggers and at no other time:** at start; on the
slow clock that already checks the curated list; on an explicit `sync()`; and on
pool exhaustion, debounced, so a pool that stays exhausted under traffic does not
storm. A rebuild re-reads the registry, the disabled map and the keys, wholesale.

**Between rebuilds a process learns availability for itself** — its own cooldowns,
its own dead keys, in memory (invariant 11). Nothing reads a peer's, and nothing
is written for a peer to read.

**Keys ride the rebuild.** One enumeration of the secrets store per rebuild
answers which refs exist; values are read on first use and dropped at the next
rebuild; a 401 drops one at once.

**A sync mirrors what a sync wrote.** No removal rule, no death evidence read
from the journal, no key-visibility evidence in the report.

## What this deletes, and what it must not

Deleted: the court at the merge, the journal as the transport for live state, the
debounced rebuild on the call funnel, the broker built per request, the per-slot
resolved key.

**Kept, and each is a place where this pass could silently take something
valuable with it:**

- **The pool-health alarm.** It lives today where membership is reconciled, which
  is exactly the seam this plan replaces. It must ride the four triggers, and the
  `ERROR` on the transition into one usable provider and into none must still fire
  ([`rules/pool-health.md`](../reference/rules/pool-health.md)). This is the only
  way an installation learns that a curated removal cost it its last provider, so
  losing it loses the safety net the mirror rule leans on.
- **The call path.** Streaming failover, the SSE chunk guards, the wait budget,
  the degraded-transport handling, the one HTTP-status vocabulary. None of it is in
  scope; it is the most-fixed code in the library and the pass must not touch it.
- **Paid models by name** — the alias resolution, the curated paid catalog, its
  cache and the wheel's floor copy.
- **The refresh** of the curated list, including the two gates and the switch that
  turns automatic fetching off.
- **Quality**: the window, the Wilson demotion, the shrinkage blend, the latency
  bound from an expired budget. All of it still derives from the journal; only
  *when* it is re-derived changes.

## Reference points

**1.3.0 is the shape, not the target.** `git show 1.3.0:src/llmbroker/broker/catalog.py`
is the registry-as-mirror this pass returns to (125 lines, and its own docstring
says "the registry is a pure mirror of a preset"). What 1.3.0 does not have is what
this pass keeps: paid models by name, the refresh, and the call-path fixes.

**`git diff 1.3.0..HEAD --stat -- src/` is the checklist.** 60 files. Every one
gets an explicit verdict — keep, delete, coarsen — and the verdict for the files
this plan does not name is *keep*. Target: `src/` around 5,500 lines from 7,691;
tests drop by roughly 3,500–4,000 lines of the ~9,000 added after 1.3.0.

## Why

Four entries are already in `decisions.md`, written with the anchors:
[`size-is-part-of-the-mission`](../reference/decisions.md#size-is-part-of-the-mission),
[`availability-is-not-shared`](../reference/decisions.md#availability-is-not-shared),
[`a-sync-mirrors-what-a-sync-wrote`](../reference/decisions.md#a-sync-mirrors-what-a-sync-wrote),
and the amended
[`learning-from-the-journal`](../reference/decisions.md#learning-from-the-journal).
This plan argues none of them again.

One entry is still owed and lands in batch 3, with the code it explains:

### the-broker-is-the-installation-a-caller-is-a-scope

A broker owns the installation: the ports, the pool, everything learned, the slow
clock, the HTTP client. What a request holds is a caller — the scope it writes on
its journal rows and the keys it may pay with, over that one shared pool.

**Blocks:** `scope` as a constructor argument; a broker built per request or per
user; a pool, learner or HTTP client per scope; `scope` as a per-call argument on
the broker.
**Why:** everything a broker owns is installation-global by invariant 16, so a
second broker for a second user duplicates every read and every connection while
duplicating no state that differs. It also breaks what the pool is for: slot
counters are what hold a provider to its `parallel` cap, and one counter per user
is not a cap. It is what makes the four triggers affordable at all — a rebuild per
process per day is nothing, a rebuild per request is a different library. Scope
reaches exactly two things, which key pays and what the journal row is attributed
to, and both are properties of the caller rather than of the installation. Passing
`scope` per call instead would put a key resolution in the middle of every call
signature and leave the pool unable to tell whose key it is holding.
**Accepted cost:** two objects where hosts previously had one.

## Work order

`. ./activate.sh` first. After **every** batch: `invoke pre` clean and
`python -m pytest` green, `N passed`, zero skips. Docker up for the testcontainer
and LocalStack/Vault tests.

Each batch carries its own spec and doc edits — never a sweep at the end.

### 1. The merge becomes a mirror

- `broker/merge.py`: the four-step removal rule, the death-evidence journal read
  and the orphan/retirement evidence go. What remains: partition on who wrote the
  entry, replace/add/remove the sync's own entries against the arriving list,
  refuse a result with no entries over a non-empty registry, refuse a duplicate
  name, bootstrap secrets from the environment, seed the disabled map.
- `broker/keys.py` deletes whole; `have_keys` goes from every signature that takes
  it; `KeyProbe` and its call site go.
- `broker/report.py`: `kept`, the retirement evidence, `keys_visible`,
  `keys_scoped` go. `SyncReport` keeps added / updated / removed / missing keys
  with their help text.
- Tests: `test_merge.py` loses the removal-rule cases and gains the mirror ones —
  an entry absent from the arriving list is removed even though its key resolves,
  and an entry the installation wrote survives that same sync. `test_death_evidence.py`,
  `test_keys_probe.py`, `test_report.py`'s evidence cases delete.
- Specs: `rules/sync-merge.md` — the removal-rule and death-evidence sections go,
  the partition and the structural guard stay; drop the now-dangling "It yields
  invariant 11" clause. Docs: the sync section of `docs/src/en/server.md` and its
  Russian copy lose "kept".

### 2. Availability stops being shared

- `broker/learning.py`: the cooldown and dead-key derivation from the journal tail
  goes; the observer applies its own findings in memory and journals the row. The
  key hash on a call row goes with it, and `models.py` loses the field.
- `journal_policy.py` / the store drivers keep quality retention and the tail read
  — quality still re-derives — so the tail read survives with one caller: the
  rebuild.
- Tests: `test_cluster_cooldown.py` inverts into `test_availability_is_local.py` —
  two brokers on one sqlite file, a 429 on one does **not** withdraw the model on
  the other, and the second learns it on its own first failing call. That is the
  accepted cost asserted rather than left to a reader.
- Specs: `rules/journal.md` — "One tail read derives everything" becomes the
  quality-only read; the shared-cooldown and key-hash sentences go.

### 3. One broker, callers per request

This is `one-broker-many-callers`, folded in whole; that file is gone and its
substance is here.

- New `broker/keyring.py`: `KeyRing` resolves `api_key_ref` → key for one scope,
  over the shared ring as fallback — `resolve`, `get`, `forget`, `set`. The ring
  belongs to the broker, keyed by scope, and the map is bounded.
- `broker/pool.py`: `_Slot.key` goes; `add` loses the key argument; `has_key` and
  `resolved_key` go; `acquire` takes `payable: frozenset[str]`, which replaces
  `s.key is not None` in the candidate filter and drives the `no_keys` exhaustion
  reason.
- New `broker/llms.py`: `AsyncLLMs` holds router, pool, its ring and its scope, and
  carries `ask`, `chat`, `stream`, `direct`, `get`, `count`, `record_quality`.
  Built by the broker only.
- `Router` loses `_scope`; `ask`/`chat`/`stream` take the caller; one `httpx`
  client stays on the router for every caller. A 401 drops the key from the ring
  that handed it over, where the attempt failed.
- `AsyncBroker.llms` and `for_scope(scope)`; `scope=` goes from both constructors;
  the empty string is refused in `for_scope`. The broker keeps
  `ask`/`chat`/`stream`/`direct`/`get`/`count`/`record_quality` as delegation, so
  the one-liner of requirement 7 never grows a second noun. `sync.py` mirrors it
  through `_run`, `direct()` included — no private reach into the async side.
- Tests, new `test_callers.py`: two callers routing on their own scoped keys and
  writing their own scope; fallback to the shared key; creating a caller performs
  no port I/O, asserted with counting ports across many `for_scope` calls;
  `parallel=1` holding across two callers; one HTTP client for many callers; a dead
  key dropping one caller's resolution only.
- `scope=` appears in ~103 places across 15 test files — grep, do not trust the
  count.
- Specs: the entry above into `decisions.md`; `rules/journal.md`'s scoping section
  (a caller is one scope's view, not a broker); `rules/call-path.md` (the
  `parallel` cap is per pool, and there is one pool per process).

### 4. Four triggers, and keys that ride them

- `broker/catalog.py`: one `rebuild()` — registry, disabled map, keys, pool
  membership, quality from the tail — and no mode flags. Its callers are exactly
  four: provisioning, the slow clock in `refresher.py`, `sync()`, and pool
  exhaustion in the router, that last one behind its own debounce.
- The exhaustion trigger is the reactive path: it is what makes a key an admin has
  just stored work without waiting out the slow clock, and the debounce is what
  stops a broken pool under load from asking every call.
- `protocols/secrets.py`: an optional enumeration method — given a prefix, the refs
  held under it. A backend without it is asked ref by ref.
  - `aws/secrets.py`: ListSecrets filtered by prefix, following pagination.
  - `vault/secrets.py`: LIST under the mount path. **The ref must become one path
    segment** — a scoped ref is `scope/REF` today, so `llmbroker/scope/REF` would
    make LIST return directory names instead of refs. Flatten on write and read it
    back the same way; no published users, so this is a change, not a migration.
  - `backends/ports.py`: one prefix query. `standalone` env secrets stay as they
    are — the lookup is free.
- Values are read on first use and dropped at the next rebuild, so a value an
  admin replaces is picked up within one slow-clock period. Nothing a listing or a
  read raises reaches the caller: it is logged once per ref and the ring answers
  from what it holds.
- **The alarm rides the rebuild**, and its dedup on the set of missing refs stays.
- Tests: `test_rebuild_triggers.py` — an idle broker reads nothing across a period
  longer than the clock, with automatic fetching off so the other clock cannot
  answer for this one; N successful calls produce no rebuild at all; exhaustion
  triggers one and a second exhaustion inside the debounce does not; an explicit
  `sync()` always does. `test_keyring.py` over a counting fake secrets backend: a
  held value read once, a ref absent from the enumeration costing no read however
  many callers ask, a listing and a read that raise leaving the ring answering from
  what it holds, a 401 forcing a re-read. `test_secrets.py` over the existing
  LocalStack and Vault containers: the enumeration returns exactly the refs under
  the asked prefix, and a scoped ref survives the Vault flattening both ways.
- Specs: `rules/list-refresh.md` gains the four triggers and is renamed (below);
  `rules/backends.md`'s secrets section gains the enumeration and the one-segment
  ref; `rules/pool-health.md` — the measure follows the last rebuild.

### 5. The rule files: ten become five

Only now, when the code they describe is final. `rules/` becomes:

| file | absorbs |
|---|---|
| `call-path.md` | unchanged |
| `selection.md` | `pool-health.md`, and availability from `journal.md` |
| `model-list.md` | `sync-merge.md`, `list-refresh.md`, `presets.md` |
| `direct-by-name.md` | `direct-aliases.md` |
| `backends.md` | the journal's read path, retention and scoping |

`invariants.md`'s index table is rewritten to the five, and so is the table in
`CLAUDE.md`. Every inbound link is re-pointed — grep for each old filename and for
each section anchor; nothing may dangle. The banned coined word for the model list
goes from every file name, section anchor, program string and doc page in the same
batch: `grep -rin lineup .` must return nothing outside `git log`.

### 6. The audit against the checklist

- Walk `git diff 1.3.0..HEAD --stat -- src/` file by file and record the verdict
  for each in this plan's `## Handover`. A file that is neither named in a batch
  above nor justified as *keep* is the residue this pass exists to prevent.
- `docs/`: the multi-user section of `server.md` and its Russian copy rewritten
  around four examples — a script with no database; a long-lived process with one;
  a FastAPI cluster with shared keys; the same with per-user keys, where only the
  dependency differs. The lifespan example builds the broker where the application
  builds its database engine, and the handler receives `llms`. State plainly that
  processes do not coordinate, that a peer's registry edit arrives at the next
  rebuild, and that a key stored while the pool is exhausted is picked up by the
  next call.
- Record the final `src/` and test line counts against the targets above.

## The gate

`invoke pre` clean and `python -m pytest` green after every batch. **Never bump
the version** — the maintainer does that by hand. Never commit unasked.

## Handover

Written when the pass lands, and it must carry: which batches are done, the
per-file verdicts from batch 6, what was done differently from this plan and why,
what was deliberately left out, and the gate results.

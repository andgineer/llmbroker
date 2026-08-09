# Who owns a registry entry, and who says what it follows

## Goal

Two properties the library does not have today, and needs together:

1. **A sync touches only what a sync wrote.** An entry the installation put in
   its registry itself survives every merge — never removed, never overwritten.
   This makes "the curated free pool *plus* two endpoints of my own, routed
   together" a supported configuration; today the first sync deletes the host's
   entries under the removal rule.
2. **Whoever built the registry says what it follows.** A broker given nothing,
   or given a connection string, keeps following the curated preset by default —
   the installation is llmbroker's, only its storage moved. A broker handed a
   registry *object* is holding content the host owns, and there `sync` must be
   stated explicitly.

Together they close a trap that exists today from both sides: a host that fills
its own registry and leaves the default alone has its pool destroyed on the
first check, and a host that wanted the curation gets it silently or not at all.
After this plan, forgetting the argument raises instead of destroying, and
stating it wrongly adds models instead of deleting them.

## Why

The two `decisions.md` entries below carry it. Nothing else in this plan argues.

Neither mechanism is recorded in `decisions.md` today; `zero-config-default` and
`the-lineup-file-is-not-a-path-a-host-names` are adjacent and are amended by the
work order rather than contradicted.

## The two entries, to land in `decisions.md` verbatim

### a-sync-touches-only-what-a-sync-wrote

Every registry entry records whether a sync put it there. A merge partitions on
that record: entries a sync wrote are the ones the removal rule may retire and
the arriving lineup may replace; an entry the installation stated itself is
carried through untouched, whatever the arriving lineup says. The default for a
new entry is *not written by a sync*, so anything reaching a registry by any
other route — a host's own mirror call, a hand-written row, a backend filled
before llmbroker ever ran — is protected without doing anything.

**Blocks:** deciding what a merge may remove from where the registry came from,
or from whether the broker was constructed with an object; inferring ownership
from the entry's shape; a per-registry "read-only" flag.
**Why:** ownership is a property of the entry, not of the backend class or the
constructor call. A host may implement a driver of its own that holds our
curated pool, and may equally pass a connection string to a database it filled
by hand — the construction path predicts nothing. It also makes the mixed pool
statable: the routed pool is whatever the registry states as pool members,
whether it came from the curation, from the host, or from both.
**Accepted cost:** one more fact stored per entry. It rides in the metadata
column that already carries the optional fields, so no schema changes; in the
lineup file the fact is structural already — `[[llms]]` is written by a sync,
`[[custom]]` is not — so the file format does not change either. An entry that
follows a paid-catalog alias is the one thing a refresh still rewrites without
having written it: the host asked for exactly that when it named the alias
instead of pinning a version.

### who-builds-the-registry-states-what-it-follows

A broker constructed with no registry, or with a connection string, follows the
curated preset by default: the installation is llmbroker's own and the string
only says where to keep it. A broker handed an already-constructed registry
object must be told what that registry follows — a preset name, or nothing at
all — and refuses to be built otherwise.

**Blocks:** a silent default for a host-supplied registry, in either direction;
deriving the default from the registry's contents; requiring the argument in the
zero-config or connection-string forms.
**Why:** in the object form both silent readings are wrong — a host that wanted
the curation would quietly not get it, and a host that did not would quietly get
its pool mixed with ours. One error message prevents both, and it fires at
construction, where the caller is looking. Deriving the default from what the
registry already holds was rejected for making the same call behave differently
on an empty and a populated database: a host that adds an entry of its own a
year later would silently lose the refresh.
**Accepted cost:** a cluster that constructs its registry object by hand writes
the preset name once in the factory it already has. The connection-string form —
what the docs use for that case — is unaffected.

## The shape

| entry | routed by the pool | rewritten or removed by a sync |
|---|---|---|
| written by a sync, pool member | yes | yes |
| stated by the installation, pool member | yes | **no — new** |
| stated by the installation, reached by name | no | no, beyond the alias it follows |

The two axes are independent after this plan: *who put it here* decides what a
merge may do to it, *how it is called* decides whether the pool routes it. Today
one field answers both, which is why the second row has no representation.

## Work order

Four batches. Each ends green.

### 1. The entry records who wrote it

1. **`models.py`.** `LLMConfig` gains the fact, defaulting to "not written by a
   sync", and it serializes into the metadata blob only when true — the same
   only-non-default rule the existing optional fields follow. Update the
   round-trip doctests.
2. **`standalone/registry.py`.** `parse_lineup` already reads `[[llms]]` and
   `[[custom]]` through one entry builder with a per-section flag; the new fact
   is set from the same section split. The file format gains nothing.
3. Confirm the identity gate: a new persisted field joins the sync identity
   comparison automatically (`specs/plans/README.md` standing rules), so this
   owes a test, not a mechanism.

### 2. The merge partitions on it

4. **`broker/merge.py`.** `merge_upstream` partitions `current` on *written by a
   sync* instead of on *reached by name*. Entries the installation stated itself
   are carried through with the same treatment custom entries get now: never
   removed, never replaced by an arriving entry of the same name, never counted
   in `added`/`updated`/`removed`.
5. **The name clash.** A host entry and an arriving curated entry may now collide
   on `name`. The merge already refuses a clash it creates; keep that, and make
   the message name the host's entry as the one to rename — the curated name is
   machine-formed and will be formed again.
6. **The report.** `SyncReport` keeps counting curated entries only. `active_before`
   / `active_after` count the live pool, host entries included — they are pool
   members.

### 3. The two worlds

7. **`broker/broker.py`.** `sync` takes a module-level sentinel as its default
   (`__repr__` of `<default>`, so the signature reads). Three branches: sentinel
   plus a registry object → `ValueError` naming both ways out; sentinel plus
   anything else → the curated preset; an explicit value → itself. The same
   signature change in `sync.py`.
8. **Tests.** Roughly 54 constructions pass a registry object with no `sync=`
   and get `sync=None` explicitly. This is mechanical, and it removes today's
   implicitness where a test silently has the curation enabled and passes only
   because the lazy refresh never fired.

### 4. Specs, docs and the queue

Listed below; part of the work, not a sweep after it.

## Tests

- A host entry in the registry survives a sync that adds, updates and removes
  curated ones, and appears in none of the report's tuples.
- The same entry is routed by the pool and failed over to.
- An arriving curated entry with the same name as a host entry is refused, and
  nothing is written.
- The identity gate: a stored entry differing from the arriving one only in who
  wrote it is not "unchanged".
- A registry object with no `sync=` raises at construction, and the message names
  both a preset name and `None`.
- The three defaulting forms: nothing, a connection string, an object plus an
  explicit preset name.
- `sync=None` with a registry object never goes to the network.

## Spec updates

- **`mission.md`**, two passages:
  - *The routed pool is exactly the curated lineup* — the sentence protects
    "a model reached by name is never routed onto"; it states that as provenance
    from our curation, which this plan makes untrue. Rewrite to say the routed
    pool holds what the registry states as pool members, that naming a model to
    call it is a different act from putting an endpoint in the pool, and that
    where the pool members came from — our curation, the installation's own
    registry, or both — is the installation's business.
  - *The lineup keeps itself current* — drop the "and stops following the
    curation" clause added by `curated-source-only`; it described the trap this
    plan removes. State instead that a refresh removes only what a refresh
    added and never rewrites what the installation stated itself, beyond the
    alias it asked us to follow.
- **`invariants.md`** — one new entry (22 of ~25): *a sync never removes or
  overwrites an entry it did not write*. Its violation is silent and it spans
  the merge, the registry ports and every backend, which is the test for this
  file.
- **`rules/sync-merge.md`** — the removal rule gains its partition; the
  paragraph added after `curated-source-only`'s review ("the merge writes into
  the registry the broker was built with… a host that wants its own pool left
  alone stops following the curation as well") goes: it describes the trap, and
  the trap is gone.
- **`rules/lineup-refresh.md`** — `sync=` is described there; add the object-form
  requirement in one sentence.
- **`rules/backends.md`** — the source-parameter dispatch paragraph: a connection
  string moves the storage, an object moves the ownership.
- **`rules/direct-aliases.md`** — verify only. A stored alias entry is still
  rewritten by a refresh, and that is now the one stated exception; say so where
  the alias contract already is.

## Docs (en and ru, in step)

- `usage.md` — the four-line table of the two worlds, stated once, where the
  section on where the model list lives already is. **The `sync=None` warning
  block added after `curated-source-only`'s review is deleted**, not rewritten.
- `server.md` — same deletion; the shared-database story is unchanged, since the
  connection-string form keeps its default.
- `direct.md` — one line: an endpoint of your own can now be a pool member by
  putting it in your registry, and it is never touched by a refresh; a model you
  reach by name is still never routed onto.

## The queue

This goes before the skeletons: it changes `models.py`, `merge.py` and the
broker constructor, which all three would otherwise be planned against.

## Gate

`invoke pre` clean and `python -m pytest` green after each batch. Docker up for
the testcontainer tests.

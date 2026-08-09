# A model reached by name is declared in code, never stored

## Goal

One field, `custom`, answers two questions at once — *is this routed* and *is
this reachable by name* — and a third fact, whether an entry follows a paid
alias, is read off the presence of an `alias` string. Nothing states the two axes
independently, so meaningless combinations are representable and are held out by
rules rather than by the shape: an alias may not sit on a pool member, an alias
may not appear outside one file section, and a refresh rewrites an entry it did
not write. Each rule exists to hold up the one before it.

The combinations disappear if a named model is never stored. It already has a
home in code — `direct=` takes a paid-catalog alias as a string and a fully
stated model as an `LLMConfig` — and that surface has no invalid state to
prohibit: the argument's type *is* the kind. What keeps the stored half alive is
one CLI command that writes into a file section that exists for it alone.

After this plan a registry holds pool members and nothing else, a named model is
declared where the application that calls it lives, and the CLI shows the curated
catalogs instead of writing into a lineup.

## The four kinds, and where each is stated

The vocabulary the docs and the rule files use from here on. Two axes, both
carried by *where the entry is stated* rather than by a field:

| kind | stated by | parameters from |
|---|---|---|
| `pool_preset` | a sync into the registry | our curated preset |
| `pool_custom` | the installation, into its own registry | the installation |
| `direct_preset` | `direct=["opus"]` | our curated paid catalog |
| `direct_custom` | `direct=[LLMConfig(...)]` | the installation |

The first word is the class — routed anonymously, or reached by name. The second
is who supplied the parameters. Both are legible from the call site, and no
combination outside these four can be written down.

Only the second word is ever recorded, and only where both values can occur: a
registry holds pool entries, so the class is not a fact about a stored row, and
in the declared form both halves are the type of the argument. The recorded half
is today's `synced`, renamed in this plan to say which of these four columns it
answers rather than how the row arrived.

## Why

The two `decisions.md` entries below carry it. Nothing else in this plan argues.

`declared-models-are-not-stored` already blocks the reverse direction and is
amended by neither entry — it is extended from "a declared model is not
persisted" to "there is no stored named model to persist it as".
`the-paid-catalog-is-curated-too` is untouched: the catalog stays, and this plan
only changes what the CLI does with it.

## The two entries, to land in `decisions.md` verbatim

### a-model-reached-by-name-is-declared-in-code

A model an application calls by name is stated where that application is
configured: `direct=` takes a curated alias as a string, or a fully stated model
as a config object. The registry holds pool members only. The CLI shows what the
curated catalogs carry and writes nothing.

**Blocks:** a stored entry reachable by name; a lineup-file section for one; a
command that adds a model to a lineup; a pinned entry written by tooling.
**Why:** the stored form buys one thing — reaching a model by name without
touching application code — and pays for it with an entry class that is
routed-or-named, curated-or-stated, followed-or-pinned in every combination the
storage can express, of which half are meaningless and are held out by rules that
must each be enforced at every write and every read. The declaration form encodes
the same choice in the type of an argument, where an invalid combination cannot
be typed. A deployment that wants the name without a redeploy is asking for
configuration outside its own configuration, which is what a registry entry is
for pool members and is not for a model one line of code calls by name.
**Accepted cost:** an installation that reached a paid model by name after
running one command now writes one line where it constructs the broker, and a
cluster writes it once in the factory it already has. The catalog is still what
supplies the alias, and the CLI still prints it.

### the-kind-of-an-entry-is-not-a-stored-field

One fact is recorded on an entry: whether our curated preset supplied its
parameters. Which class it belongs to — routed, or reached by name — is not, and
neither is the combination of the two. A stored entry is a pool member because a
registry holds nothing else; a declared one is followed or stated by the type of
the argument that declared it.

**Blocks:** a `kind` enum on the entry; **a pair of booleans for class and
source**; a per-entry flag for reachable-by-name.
**Why:** an enum would carry four values of which storage could ever hold two,
and the other two would exist to be validated against — the shape this plan
exists to remove, re-introduced under a better name. The boolean pair is the same
objection arithmetic: the class bit is constant in every row that could hold it,
so it carries no information and buys no filter, because nothing after this plan
holds routed and named entries in one collection to filter. What remains is one
bit, and it is the one the merge already partitions on.
**Accepted cost:** a host reading an entry cannot ask it what kind it is. Nothing
does — the pool takes what the registry holds, `direct()` searches what was
declared, and the merge partitions on the one recorded bit.

## Work order

Six batches. Each ends green.

### 1. The command shows instead of writing

1. **`cli.py`.** `add-model` and everything reached only from it goes: the
   interactive provider/model prompts, `--pin`, `--name`, the append, the
   collision check against the lineup.
2. A `list` command replaces it, read-only, no arguments: the curated pool
   preset and the paid catalog, each provider with its models. A paid model's
   line carries what a caller needs to reach it — the alias for `direct=["…"]`
   and, for a pinned declaration, the provider id, model id, `base_url` and
   `api_key_ref`.
3. Output shape is fixed now even though the filters are not built: one item per
   line, no decoration, so a later `--aliases` is a projection of the same rows
   rather than a second formatter.

### 2. The lineup file has one array

4. **`standalone/registry.py`.** `parse_lineup` reads `[[llms]]` only. A file
   carrying `[[custom]]` is refused with a message naming `direct=` — silently
   dropping entries a previous release wrote would take models out of an
   installation without saying so. The alias refusal in `config_from_entry` goes
   with the section it guarded.
5. **`broker/lineup_file.py`.** `render_lineup` emits one array. `_entry_dict`
   stops writing `alias`.
6. **`broker/presets.py`.** The check that a curated lineup declares no
   `[[custom]]` entries becomes the general one: a lineup has no such array.

### 3. The field goes

7. **`models.py`.** `LLMConfig.custom` is removed, with its metadata
   serialization. `check_aliases` loses the pool-member rule and keeps
   uniqueness, which is what it is for.
8. **`broker/catalog.py`.** `_reconcile` takes the stored configs and the
   declared ones as two arguments instead of filtering one list; the pool is the
   stored list. `find_custom` searches the declared list and is renamed for what
   it now does. `check_overlay` keeps its job — a declared model must not claim a
   handle the registry already uses — unchanged.
9. **`broker/aliases.py`.** The two `custom=True` assignments go.
10. **`broker/merge.py`, `sync.py`, `exceptions.py`.** Prose and messages that
    name `[[custom]]`. `PoolModelError`'s hint now points at `direct=`.

### 4. The recorded bit is named for what it answers

`synced` says how the row arrived. What every reader of it actually asks is
whose parameters these are — which is the second column of the table above, and
the reason `custom` drifted was the same substitution of mechanism for meaning.
Mechanical, and the whole inventory is 12 sites in `src/` and 20 in `tests/`.

11. **`models.py`.** The field and the metadata key become `from_preset`.
    Serialization stays only-when-true, and the doctest moves with it.
12. **`broker/merge.py`.** Five partition sites — `merge_upstream`,
    `retirement_candidates` and the arriving/stored/owned split.
13. **`standalone/registry.py`.** The parser sets it from the one array it now
    reads, so what was `synced=not custom` becomes an unconditional true: a file
    is llmbroker's own output, and everything in it came from a preset.
14. **Tests.** Ten files, `test_registry_ownership_conformance.py` included —
    its provenance axis writes the metadata key as a literal, and the row that
    names the key is the point of the cell.

### 5. The alias refresh moves to where an alias now lives

The merge re-points stored alias entries. There are none left, so that half is
dead code and the facts it produced have no producer — but the event it reported,
"the model behind an alias moved", still happens, at the declared re-resolution.
The machinery moves rather than going away.

15. **`broker/merge.py`.** `refresh_alias_configs` and the alias-target argument
    threaded into `merge_lineup`/`merge_upstream` go. `SyncReport` stops carrying
    alias facts; the sync still reads the catalog on its own clock, which is what
    keeps a declared alias current, and that read stays.
16. **`broker/aliases.py`.** `resolve_declared` produces the facts instead: for a
    re-resolution, one per declared model whose model id or `api_key_ref` moved.
    `AliasFact`, `AliasChange` and `alias_lines` keep their shape and change
    producer; the first resolution has nothing to compare against and produces
    none.
17. **`broker/broker.py`.** The re-resolution logs them, on the path that already
    logs the catalog refresh. A version move is one line, and it is the only
    notice a deployment gets that its `direct("opus")` now answers from a
    different model.

### 6. Specs, docs and the queue

Listed below; part of the work, not a sweep after it.

**Every user-facing string this plan writes says "the model list", never
"lineup"** — the `list` output, the `[[custom]]` refusal, `PoolModelError`'s
hint, the rewritten doc sections. `model-list-vocabulary` removes the coined word
from everything a reader sees and is taken after this plan; writing it now would
only add lines to its inventory. Identifiers are untouched either way — that
plan's boundary keeps `lineup.toml`, `parse_lineup` and the rest.

## What this closes, and what it does not

Closed, without a rule of its own left behind:

- *an alias may not sit on a pool member* — a pool member has no alias field to
  put one in. The check in `Catalog._reconcile` goes with it, and so does the
  finding against it from plan 9's second review.
- *an alias may appear only in one file section* — there is one array.
- *a refresh rewrites an entry it did not write* — the alias refresh touches
  declared models, which are not stored, so the exception in
  `a-sync-touches-only-what-a-sync-wrote` becomes unnecessary and its sentence is
  dropped.

Not closed, and out of scope: an alias whose model the provider withdraws. That
is plan 16, which this plan does not overlap — it operates on the declared half,
which survives here unchanged.

## Tests

- `list` prints every provider of both curated presets, and every paid model's
  alias; it writes nothing — assert the lineup file is untouched.
- A paid model's line carries the four fields a pinned declaration needs.
- A lineup file with `[[custom]]` is refused, and the message names `direct=`.
- A rendered lineup round-trips through the parser with one array.
- A curated preset carrying `[[custom]]` is refused as before, by the general rule.
- The pool is exactly what the registry holds; a declared model never joins it.
- `direct()` resolves a declared alias and a declared config, and raises for a
  name that is only in the registry.
- Two declared models colliding on a name or alias with the registry still raise
  from `check_overlay`.
- An alias carried twice among declared models is refused; a pool member cannot
  carry one, and there is no test for it because there is no field.
- The registry round trip on every backend, with the metadata blob one key
  shorter.
- A sync over a registry holding an entry named like a catalog alias changes
  nothing about it — the merge no longer follows aliases at all.
- A re-resolution whose catalog moved a declared alias to another model id logs
  one line naming both; the first resolution logs none.
- The renamed bit round-trips through the metadata blob on every backend, and a
  row that does not carry the key still reads as the installation's own — the
  property the conformance matrix's provenance axis exists to pin, and the whole
  matrix must stay green through the rename with only its literals changed.

## Spec updates

- **`rules/direct-aliases.md`** — the largest change. The stored half goes: no
  `[[custom]]` entry, no `add-model`, no "a followed entry is not the host's to
  hand-edit". What stays is the alias contract itself, and it now describes one
  form — declared in code, re-resolved on the refresh clock. The four kinds and
  where each is stated go here, in the table above.
- **`rules/presets.md`** — the CLI section: `list` replaces `add-model`.
- **`rules/sync-merge.md`** — the lineup file has one array; the removal rule's
  partition is unchanged. The report section loses the alias facts: a sync no
  longer follows an alias, it only keeps the catalog current for the resolution
  that does.
- **`rules/lineup-refresh.md`** — the catalog's own refresh clock is now the only
  thing a declared alias rides on; the sentence describing it stays and is no
  longer shared with a stored half.
- **`decisions.md`** — the two entries above, verbatim.
  `declared-models-are-not-stored` gains one clause pointing at the first.
  `a-sync-touches-only-what-a-sync-wrote` loses the followed-entry exception from
  its accepted cost. **Its anchor stays** — the rule it records is still "a sync
  touches only what a sync wrote", and the rename changes the field the code
  partitions on, not the rule. Renaming the anchor would break every link into
  it and buy nothing.
- **`invariants.md`** — entry 4 says "nothing a host declares *in code* enters
  the routed pool… an entry is pooled exactly when it is not one reached by
  name". The second half is now true by construction and stops being a rule: the
  entry shrinks to its first sentence. Entry 22 is untouched — a rule about what
  a sync may do is correctly stated in terms of syncs, and neither entry names a
  field, which is why the rename does not reach this file. No new entry; the file
  stays under its cap.
- **`mission.md`**, one passage: *The routed pool is whatever the registry states
  as pool members* says reaching a model by name means pinning a version or
  naming a permanent alias. Add that both are stated where the application is
  configured, and that the registry holds pool members only. Intent, not
  mechanism — no field names, no sections.

## Docs (en and ru, in step)

- `direct.md` — rewritten around the declared form. The `add-model` walkthrough
  and the lineup-section prose go; what replaces them is `list` for finding the
  alias and one `direct=` line for using it.
- `usage.md` — the passages naming `[[custom]]`; the own-entry section from plan
  9's review stays and is now the only way an entry reaches a registry by hand.
- `server.md` — the cluster case: a paid alias is written once in the factory
  that builds the broker.

## The queue

Before the classification question and before the skeletons. It removes a field
and a CLI command that 12 and 14 would otherwise be written against, and it
decides — by removing the storage — whether a kind field is needed at all, which
is why no plan for one is queued. Independent of 10, 11 and 16.

## Gate

`invoke pre` clean and `python -m pytest` green after each batch. Docker up for
the testcontainer tests.

## Handover

### Done

All six sections. The gate is green: `invoke pre` reports no ruff/pyrefly errors,
`python -m pytest` is `1273 passed`, zero skips, zero errors (Docker up, so the
Postgres/Mongo testcontainer cells of the conformance matrix ran).

- **1. The command shows instead of writing** — `add-model` and everything reached
  only from it are gone, including the `EOFError`/`KeyboardInterrupt` guard in
  `main`, which existed only for the interactive prompts. `list` replaces it:
  one model per line, `pool` lines then `direct` lines, no decoration. A paid line
  is `direct <alias> <provider id> <model id> <base_url> <api_key_ref>`, with `-`
  in the alias column for a catalog model that carries none. `POOL_PRESET` was
  added beside `PAID_CATALOG` in `broker/presets.py` and `broker.py`'s default sync
  source now reads it, so `"freetier"` is a literal in one place.
- **2. The model list has one array** — `parse_lineup` reads `[[llms]]` only and
  refuses a file carrying `[[custom]]`, naming `direct=[...]`. `render_lineup`
  emits one array; `_entry_dict` no longer writes `alias`. The file header now
  points at `direct=[...]` instead of `add-model`.
- **3. The field goes** — `LLMConfig.custom` and its metadata serialization are
  gone. `Catalog.entries()` returns the stored pool and the declared models as two
  lists; `_reconcile` takes both; `find_custom` is now `find_declared(stored,
  declared, alias, name)`. `check_overlay` is unchanged.
- **4. The recorded bit** — `synced` is `from_preset`, field and metadata key,
  across `src/` and `tests/`.
- **5. The alias refresh moved** — `refresh_alias_configs`, `AliasRefresh`,
  `alias_targets_for` and the `alias_targets` argument threaded through
  `merge_lineup`/`sync_lineup_file` are gone. `resolve_declared` now takes
  `previous` and returns `(DeclaredModels, facts)`; `AsyncBroker._resolve_declared`
  logs one `direct=: …` line per moved alias. The sync still reads the catalog on
  the way past — through `LineupRefresher._refresh_paid_catalog`, which the sync
  path now calls with the fetch failure caught, so a catalog nobody can reach
  cannot fail the sync of the model list.
- **6. Specs and docs** — as listed in the plan, en and ru in step. Also updated:
  `presets/paid-catalog-refresh-prompt.md`, `presets/freetier-refresh-prompt.md`
  and `paid-catalog.toml`'s header, all of which named `add-model` or `[[custom]]`.

### Done differently from the plan

- **Batches 2 and 3 were implemented as one.** Removing the `[[custom]]` section
  and removing the field break the same 43 tests; splitting them would have
  rewritten those test files twice. Each source change still landed in the plan's
  order, and the gate was run once at the end of the pair.
- **`ALIAS_NAME_HINT` was deleted** (`standalone/registry.py`, and its use in
  `merge._check_name_clash`). The plan does not mention it. It told a reader to
  "drop its 'alias' to pin it instead" after a refresh re-formed an entry's name —
  a mechanism this plan removes, so the hint is not merely dead but wrong.
- **`check_aliases` is no longer called from `parse_lineup`.** The plan keeps the
  function's uniqueness rule, and it is kept — but the file parser no longer reads
  `alias` at all, so the call there could not fire. It stays live where it can:
  `DriverRegistry.load`/`mirror`, where an alias can arrive in a hand-written
  metadata blob.
- **`AliasChange.UNKNOWN` was removed and `alias_lines` returns one tuple**, not
  a (notices, warnings) pair. The plan says the three keep their shape. UNKNOWN
  had exactly one producer — `refresh_alias_configs` — and this plan deletes it: a
  re-resolution whose alias the catalog dropped raises out of `_entry_for_alias`
  and is reported by `_resolve_declared`'s existing warning instead. Keeping the
  member would have left an enum branch no code path can reach.
- **`Catalog.entries()` changed return type** rather than gaining a sibling. The
  plan only specifies `_reconcile`'s two arguments; the two callers of `entries()`
  (`provision`/`resync` and `AsyncBroker._resolve_direct`) both need the split, so
  a second accessor would have re-read the registry.

### Decisions the plan did not make

- **`list` output shape.** The leading token is `pool` / `direct` — the words a
  caller actually types (`ask()` routes the pool, `direct=` declares) rather than
  the `pool_preset`/`direct_preset` vocabulary of the kinds table, which is spec
  language and not CLI language. A later `--aliases` is
  `awk '$1=="direct" {print $2}'` over the same rows.
- **What replaces the file-level tests that used `[[custom]]`.** In a file registry
  every entry is now `from_preset=True`, so the "an entry the installation stated
  itself survives a sync" property has no file-level expression at all — it lives
  only in a DB registry. Those tests moved to the merge and the conformance matrix
  (which already carry it), and the file tests they left behind now assert the
  *kept*-entry path, which is the one a file can still exercise.
- **The conformance matrix's `raw-only-custom` provenance cell** was kept, renamed
  `raw-unknown-key`: a metadata blob carrying only a key this release no longer
  knows must still read as the installation's own. That is the property the axis
  exists to pin, and it survives the field's removal.

### Left out

- Nothing from the plan. The vocabulary sweep (`lineup` → "the model list") is
  plan 10's and was not attempted; every user-facing string this plan *wrote* uses
  10's wording already, and the strings it did not touch were left for 10 so its
  inventory stays honest.

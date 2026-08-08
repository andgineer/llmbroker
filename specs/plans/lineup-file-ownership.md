# The lineup file is rendered, not assembled

## Goal

Stop merging an arriving lineup **into the text** of a config file. Split the
file configuration by owner, render llmbroker's half wholesale from configs, and
never open the host's half for writing. Then split `upstream.py`, which is the
1045-line file this machinery grew inside.

This is the largest plan in the queue and the only one that deletes a mechanism
rather than tidying one.

## Why

`sync_file` assembles the target from three sources: the arriving preset text
verbatim, kept entries re-emitted under a generated header, and the file's own
`[[custom]]` blocks carried as raw dicts — then verifies the result parses back
to exactly the merge (`_check_render_faithful`). That verification exists
because the assembly is hand-rolled; it is a guard against the plan's own
complexity.

The database path has none of this. It merges configs and writes configs. The
file path can have the same shape.

The reason it does not today is that one file holds three ownerships:

| entry | owned by | what sync does to it |
|---|---|---|
| `[[llms]]` | llmbroker | rewrites wholesale |
| `[[custom]]` **with** `alias` | llmbroker owns the contents | rewrites `name`, `model`, `base_url`, `api_key_ref` |
| `[[custom]]` without `alias` | the host | never touched |

Make ownership a file boundary and the question "is this entry rewritten"
becomes "which file is it in".

## The shape

| file | holds | sync |
|---|---|---|
| the named config (`lineup.toml`, or whatever path the host gave) | pooled entries **and** alias-following entries | rendered wholesale from configs |
| sibling `custom.toml` | pinned host entries | opened read-only, always |

Sibling-by-convention has precedent in the codebase: `.env` and `store/` are
already located as `registry.path.parent / …`. The public API stays
single-path — `AsyncBroker("llms.toml")` is unchanged.

A database registry has neither file. Ownership there is already a field
(`custom` in the metadata blob) and `mirror` already respects it; nothing on
that path changes.

## Work order

Blocked by `lineup-parser` — this plan assumes one `parse_lineup`.

1. **Registry reads both files.** `standalone/registry.py::Registry.load()`
   reads its own path, then the sibling `custom.toml` if present, parsing both
   through `parse_lineup` and flagging the second's entries `custom=True`
   without an alias. Uniqueness is checked across the union, not per file.
   `key_info()` merges both `[keys]` tables, the host's file winning — a hint
   someone wrote by hand outranks a curated one, which is the rule
   `Catalog.key_help` already follows.

   Note before writing this: `load()` and `key_info()` each read and parse the
   path themselves, so a single sync source already reads one file three times
   (both methods plus the raw text). Two files per method makes that five, and
   the reads are not of one instant — the halves can disagree if the file is
   edited between them. Decide here whether the pair reads once; `lineup-parser`
   left this open on purpose, since collapsing it means either caching a parse
   or changing the two-method registry protocol, and this plan is the one that
   reshapes both.

2. **Render wholesale.** One function renders a list of `LLMConfig` plus a
   `[keys]` table to TOML text. Delete `render_merged_toml`, `_keys_tail`,
   `_check_render_faithful`, `entry_block` and `_KEPT_HEADER`. Nothing is
   preserved from the previous text, so nothing has to be verified against it.

   `KeyInfo.extra` must survive the round trip — it is a documented passthrough
   (`decisions.md::keyinfo-is-a-passthrough`) and today it survives only because
   the raw table is copied verbatim. Render it back.

3. **One sync target.** `sync_file` and `AsyncBroker._sync_registry_target`
   collapse into one operation: load current configs, merge, write. The file
   writer writes text; the registry writer calls `mirror`. Everything above the
   writer is shared, which removes the duplicated five-step merge-site assembly
   between `cli.py::_sync_target` and `broker.py::_sync_file_target`.

4. **One alias refresh.** Delete `refresh_alias_entries` (the raw-dict half).
   `refresh_alias_configs` is now the only one, because both targets carry
   configs by the time the refresh runs.

5. **`KeyEvidence`.** `present`, `keys_visible`, `keys_scoped`, `have_keys` and
   `scope` travel together through `merge_upstream`, `retirement_candidates`,
   `_removal_plan`, `sync_file` and both call sites. Resolve them once into a
   frozen value object and pass that. Drops four parameters from three
   signatures and the `noqa: PLR0913` from `merge_upstream`. `SyncReport` keeps
   its two boolean fields — they are what the report explains.

6. **Preset source.** Replace `cached_preset_text`, `local_paid_catalog_text`,
   `paid_catalog_text`, `bundled_preset_text`, `refresh_cached_preset` and the
   `bundled: bool` flag threaded through five frames with one object holding an
   explicit precedence. The bundled copy **seeds the cache on first read**
   rather than being a third branch; "never roll an alias backwards to the
   wheel's copy" then falls out of the cache entry being older, not out of a
   boolean passed down five call frames.

7. **Structural alias notices.** `AliasRefresh.notices` / `.warnings` are
   ready-to-print English produced in the merge layer. Replace with typed facts
   (alias moved; key ref changed; alias unknown). The CLI formats them for
   `print`, the broker for `logger`. Same for `SyncReport.__str__`, which moves
   out of `models.py` to a renderer next to its two consumers.

8. **Split `upstream.py`.** With the above removed the file is roughly half its
   size; split what remains by subject:

   ```
   broker/presets.py        fetch, https refusal, the PresetSource of step 6
   broker/aliases.py        AliasTarget, catalog targets, resolve_declared, refresh
   broker/keys.py           KeyEvidence, present_refs
   broker/merge.py          merge_upstream, _removal_plan, retirement, check_not_emptying
   broker/lineup_file.py    load, render, write the file target
   util/atomic.py           write_atomic
   ```

   `broker/stamps.py` imports `write_atomic` from `util/atomic.py`, so the
   stamp module stops depending on the sync engine.

9. **CLI.** `cli.py` calls the shared seam from step 3. Its imports from the
   old `upstream` drop from nine symbols to two. `add-model` writes a pinned
   entry to `custom.toml` and an alias-following entry to the lineup file —
   which is the ownership rule, applied at the point of creation.

## Tests

- `tests/test_upstream.py` (879 lines) splits along the new modules.
- `tests/test_cli.py`: `--sync` against a target with a sibling `custom.toml`;
  the sibling is byte-identical afterwards. Assert on bytes — "we did not write
  it" is the whole guarantee.
- A test that a comment inside `custom.toml` survives a sync, and that the
  lineup file is regenerated without preserving its own.
- `KeyInfo.extra` round-trips through a render.
- Alias re-pointing over both targets produces the same typed facts.

## Spec updates

- `rules/sync-merge.md` — the target is now two files by owner; the merge itself
  is unchanged. State the current shape only.
- `rules/presets.md` — one precedence, bundled seeds the cache.
- `rules/direct-aliases.md` — an alias-following entry lives in the llmbroker
  file. This is the file-level consequence of what `mission.md` already says:
  such an entry is not the host's to hand-edit.
- `decisions.md` — one new entry, `ownership-is-a-file-boundary`, naming the
  alternative that was rejected: one file rendered wholesale, which costs the
  host the comments and formatting of their own entries.

## Gate

`invoke pre` clean, `python -m pytest` green after each batch — steps 1-2, 3-5,
6-7, 8-9 are four batches, not one.

---

## Handover

### What is done

All nine steps of the work order, plus the tests and spec updates the plan names.
`broker/upstream.py` is gone; what was in it now lives in:

| module | holds |
|---|---|
| `broker/presets.py` | the fetch, the https refusal, `PresetSource` (step 6) |
| `broker/aliases.py` | `AliasTarget`, catalog targets, `resolve_declared`, the refresh, the typed facts |
| `broker/keys.py` | `KeyProbe` + `KeyEvidence` (step 5) |
| `broker/merge.py` | the source, the merge, death evidence, the guard, and `merge_lineup` — the one merge site (step 3) |
| `broker/lineup_file.py` | render, write, and the file target end to end |
| `broker/report.py` | `SyncReport` → text, and alias facts → lines (step 7) |
| `util/atomic.py` | `write_atomic`; `broker/stamps.py` no longer imports the sync engine |

Deleted with nothing replacing them: `render_merged_toml`, `_keys_tail`,
`_check_render_faithful`, `_KEPT_HEADER`, `refresh_alias_entries`,
`cached_preset_text`, `local_paid_catalog_text`, `paid_catalog_text`,
`refresh_cached_preset`, `SyncSource.text`, `SyncReport.__str__`, and the
broker's `_present_refs` / `_keys_visible` / `_dead` / `_log_alias_lines`.

### Decisions the plan left open or that went differently

- **The pair reads once where it matters** (step 1's note). `read_lineup_parts`
  reads both files in one call and validates them as one lineup; the file target
  merges from that. The two-method registry protocol is unchanged and neither
  method caches, so `load()` and `key_info()` each read the pair for themselves —
  they are independent public reads and a cache would be a staleness bug. `sync()`
  still pre-reads the lineup once to collect the aliases whose catalog targets it
  must fetch; that read is not a merge input.
- **`parse_lineup` now returns a `Lineup`** (configs + keys, in `models.py`), and
  `merge_upstream` takes and returns one. The tuple-unpacking at every call site
  was the thing that made the two-file union awkward to express.
- **Two value objects, not one** (step 5). `KeyProbe` (secrets, `scope`,
  `have_keys`) is built once per merge site; `KeyEvidence` (`present`, `visible`,
  `scoped`) is what its one probe produces and what travels into the merge.
  Carrying `scope`/`have_keys` on the evidence object as the plan describes would
  have left two fields nothing downstream reads.
- **The bundled copy is not seeded into the cache** (step 6). One object holds the
  precedence, and the `bundled` flag is gone from five frames — but as
  `PresetSource.text(name, *, prefer_cache=False, floor=True)`, with each keyword
  set at the single call site whose decision it is, not by merging the floor into
  the cache. Seeding was tried on paper and rejected: a cache entry that is really
  the wheel's copy is indistinguishable from one the machine fetched, so a
  cold-cache offline sync would re-point stored alias entries *backwards* — the
  exact move `presets.md` forbids. Keeping that guarantee under seeding needs a
  marker on the cache entry and a tri-state cache read, which is more machinery
  than the flag it removes. `local()`'s cache-first order survives as
  `prefer_cache`, since a re-resolution runs right after the refresh filled the
  cache and must not fetch the same body twice.
- **`entry_block` survives**, in `lineup_file.py`. The plan lists it for deletion,
  but `add-model` appends one block to a file it must not otherwise rewrite —
  re-rendering the host's file would destroy the comments the split exists to
  protect.
- **The generated file carries a two-line header** naming itself as generated and
  pointing at `custom.toml`. Not in the plan; with the whole file now machine-
  written, a reader who opens it has to be told where their own entries go. It is
  fixed text, so the byte-level identity gate is unaffected.
- **`custom.toml` refuses two things** rather than accepting them silently:
  `[[llms]]` (the routed pool takes no host entries) and an `alias` (a
  machine-rewritten entry cannot live in a file no sync writes). Both are checked
  when the pair is read, so the CLI and the broker inherit them.
- **Each file documents its own refs.** The merged `[keys]` written into
  llmbroker's half excludes refs `custom.toml` already documents, so the generated
  file never contains a copy of the host's hint. A read of the pair merges both
  tables anyway, the host's winning.
- The registry target's alias facts are now logged *after* the merge rather than
  before it — same lines, one call site.

### Deliberately left out

- **The preset-only restriction on a file target stays.** Wholesale rendering
  removes the technical reason the plan's `Why` section names, but `sync-merge.md`
  states the rule as part of the tier model, so widening it would be a behavior
  change this plan did not ask for. The spec sentence that justified it by
  `[[custom]]` duplication has been restated.
- **No migration for an existing lineup file.** A host whose pinned entries sit
  in the lineup file keeps working — they are carried over and re-rendered — but
  their comments and any field llmbroker does not model are dropped on the first
  sync. Moving them into `custom.toml` is a hand edit, and the docs now say so.

### Behavior a reviewer should expect to see change

1. The first sync of a hand-written lineup file always rewrites it (into the
   rendered form); from there it is byte-stable. `test_a_file_broker_reports_no_change_at_debug`
   now syncs twice for that reason.
2. Comments and unknown keys in llmbroker's half do not survive a sync.
3. `add-model --pin` writes `custom.toml`, not the `--into` file. Collisions are
   checked against both files either way.
4. Cosmetic: a multi-line `help` string now renders escaped on one line rather
   than as the preset's triple-quoted block — `tomli_w` writes no multi-line
   strings, and the text is no longer copied verbatim. It parses back identically,
   and `llmbroker env` still prints it as several comment lines.

### Gate

`invoke pre` clean (ruff, ruff-format, pyrefly: 0 errors), `python -m pytest`:
1231 passed, 0 skipped, 0 errors — Docker up, testcontainer tests included.

---

## Review round 1 — fixes applied

Reviewed against this plan and `lineup-refresher`; three defects with repros, four
spec/doc gaps. All fixed in one batch. Nothing found after this batch changes
runtime behavior.

### Defects fixed

1. **A lineup file named `custom.toml` was read as its own host half**, so every
   entry came back twice and `Registry.load()` raised `duplicate name`. Worse,
   `preset <name> --sync custom.toml` exited **0** and wrote a file the registry
   then refused (`carries [[llms]]`). The host filename is now reserved:
   `host_path` raises, and `add-model --into custom.toml` is refused early with
   its own message rather than a traceback.
2. **A ref documented in `custom.toml` stripped `[keys.REF]` out of llmbroker's
   half entirely — even for a pool entry from the preset**, so the generated file
   stopped carrying the curated hint for a model it owns. The filter now drops a
   ref only when no entry in llmbroker's half uses it. The rule "each file
   documents the refs of its own entries" is what the code does now; before, it
   was "the host's file wins the whole ref".
3. **`SyncReport.__str__` was removed with no public replacement.** `usage.md`
   still documented `print(report)` and rendered its output, and `last_sync_report`
   is public surface. `format_report` is now re-exported from the top-level package
   and the docs use it. This is the one step-7 loose end `models-purity` was told
   to check for.

### Decisions recorded that were only in this handover

- **The bundled copy is not seeded into the cache** is now
  `decisions.md::the-floor-is-not-seeded-into-the-cache`, with the counter-argument
  (a seeded entry is indistinguishable from a fetched one, so a cold-cache offline
  sync re-points backwards). It would have been re-proposed once this file went.
- **A file *source* is read as the pair too** — an unremarked consequence of
  routing `load_sync_source` through `read_lineup`. Confirmed by repro: a
  `custom.toml` beside a vendored lockfile travels into the target registry. Kept,
  because splitting it would make naming a path mean different things on the two
  sides of a merge; now stated in `sync-merge.md` and `server.md`.
- **The preset-only file target** kept its restriction but lost its circular
  justification ("a hand-maintained lineup is an input to the merge" — so is a
  preset). Restated honestly in `sync-merge.md`, `cli.md`, `server.md`: not
  technical, it simply costs no use case.
- **The migration note the handover claimed the docs carried, they did not.**
  `direct.md` (en+ru) now says that pinned entries already in the lineup file keep
  working but lose their comments on the first sync, and that moving them is a hand
  edit.

### Deliberately not fixed

- `Catalog.entries()` re-reads and re-validates the registry on every `direct()`
  call — two file opens, two parses and three uniqueness passes since the split
  (638 µs vs 451 µs, 30 entries, on the event loop). The registry methods are right
  not to cache; the cache belongs to whatever owns the declared overlay, so this is
  recorded as finding 3 in `specs/plans/declared-out-of-catalog.md`.
- Cosmetics: a set comprehension rebuilt per iteration (now hoisted as part of fix
  2), `_NO_TARGETS` duplicated across three modules, triple uniqueness validation
  in `read_lineup_parts`.

### Gate after the fix batch

`invoke pre` clean (0 errors), `python -m pytest`: **1240 passed**, 0 skipped,
0 errors.

---

## Review round 2 — the split is reversed

Round 2 found one defect and, chasing it, found that the shape it lived in was
not the plan's. Both are gone; what the plan actually asked for stands.

### The defect

**A `[keys]` hint for a ref only the host's entries used was destroyed by a
sync.** Repro, with no hand-editing anywhere: `preset --sync` writes a pool entry
and its key help; `add-model --pin` on that same provider writes the entry into
the host's file but *not* the help, because the ref was already documented in
llmbroker's half; the pool later drops that provider, and the help goes with it.
`llmbroker env` then prints a bare `REF=` for the one key still missing — the one
holding back the paid model the user just added. The report's pending-key line
loses its help on the next sync too.

### Why the shape went

The rule that produced it — *each file documents the refs of its own entries* —
is not in this plan. Step 2 asks for "one function [that] renders a list of
`LLMConfig` plus a `[keys]` table"; the split of that table by owner was invented
during implementation, patched in round 1, and defective in round 2.

Removing it re-opened the question the split answers, and the answer did not
survive it: **nothing writes into the lineup file except llmbroker's own
commands.** `preset --sync` generates it and `add-model` appends to it; the two
forms a host declares — a full config and a paid-catalog alias — arrive through
that command or through `direct=` in code. There is no hand-written text in the
file, so there was nothing for the second file to protect. Worse, half the host's
own models — the alias-following ones — lived in the generated half anyway, so
the split bought comment preservation for some of the host's entries and charged
for all of them.

Two alternatives were weighed and recorded in
`decisions.md::the-lineup-file-is-generated-not-authored`: a style-preserving
TOML document library (returns the writer to editing a live file in place, the
shape this plan exists to leave) and refusing to sync a file carrying a comment
(stops a refresh the mission promises is unconditional, and llmbroker's own
presets carry comments).

### What changed against the work order

- **Step 1 is reversed.** `Registry.load()` and `key_info()` read one file.
  `host_path`, `_host_lineup`, `read_lineup_parts`, the reserved filename and the
  two refusals inside the host file are gone.
- **Step 9's ownership routing is reversed**, and `add-model` lost `--into`
  entirely: it writes the lineup inside llmbroker's own directory, which
  `LLMBROKER_HOME` already relocates. Collisions are checked against that one
  file.
- **Everything else stands**: the wholesale render (step 2), the one merge site
  (3), the single alias refresh (4), `KeyEvidence` (5), `PresetSource` (6), the
  typed alias facts (7), and the module split (8).

### Fixed alongside, independent of all this

`SyncReport.updated` counted only pool entries, so a vendored source replacing a
stored user model — a supported and tested behaviour — moved it to a different
`base_url` with nothing in the report. It now counts the host's entries too.

### Gate

`invoke pre` clean (ruff, ruff-format, pyrefly: 0 errors), `python -m pytest`:
1232 passed, 0 skipped, 0 errors.

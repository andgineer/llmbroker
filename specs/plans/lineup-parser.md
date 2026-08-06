# One lineup parser

## Goal

Lineup entries are parsed in two places with **different validation**. Make one
parser and route both readers through it.

## Why — this one fixes a real divergence

| reader | parses | validates |
|---|---|---|
| `standalone/registry.py::Registry.load()` | file → configs | `_check_unique_names` **and** `check_unique_aliases` |
| `broker/upstream.py::configs_from_data()` | parsed dict → configs | **neither** |

`upstream.py::key_infos_from_data` (870-874) is additionally a verbatim copy of
`Registry.key_info()` (132-137).

Consequence: a source file carrying the same `name` twice is refused when read
as a registry and accepted when read as a sync source. On the sync path the only
guard is `_check_name_clash`, which runs on the *merge result* — so a duplicate
that the merge happens to collapse is never reported at all. Two answers to
"is this file valid" is not a style problem.

## Work order

1. **Write the repro test first, and keep it.** In `tests/test_registry.py` (or
   a new `tests/test_lineup_parser.py`): a TOML carrying one name twice across
   `[[llms]]` and `[[custom]]`, fed through the sync path, must raise the same
   `ValueError` that `Registry.load()` raises. It fails before the fix. Do not
   delete it afterwards — it is what proves the divergence is closed.

2. **`standalone/registry.py`** gains the single parser:

   ```python
   def parse_lineup(data: dict) -> tuple[list[LLMConfig], dict[str, KeyInfo]]:
   ```

   Body is today's `Registry.load()` from `data.get("llms", ...)` onward, plus
   `key_info` — including `_check_unique_names` and `check_unique_aliases`.
   Section order (`llms` then `custom`) is preserved: it is the pool's curated
   order and `Catalog._reconcile` passes it to the pool as `order=`.

3. **`Registry.load()` / `Registry.key_info()`** become `_read_data` +
   `parse_lineup`. `key_info()` must not re-validate — it reads the same file
   and would double the work on every call; have both read one parse.

4. **`broker/upstream.py`** — delete `configs_from_data` and
   `key_infos_from_data`; every call site takes `parse_lineup`. Sites:
   `sync_file` (two calls), `load_sync_source`, `_check_render_faithful`,
   and `cli.py::_sync_target` (two calls).

5. **Watch the fetched-preset path.** `load_sync_source` parses the downloaded
   preset text. A curated preset with a duplicate name will now raise where it
   previously passed — which is correct (that preset is broken) but it must
   fail as a `ValueError` the refresh already catches, not as something that
   stops a process. Confirm `_attempt_sync`'s `except (ValueError, OSError)`
   covers it; it does today.

## Tests

- The repro from step 1.
- A duplicate `alias` across `[[custom]]` entries, same treatment.
- `tests/test_registry.py`, `test_upstream.py`, `test_cli.py` otherwise green.

## Spec updates

`rules/sync-merge.md` — if it states the merge-result name-clash check as the
only uniqueness rule, correct it to say uniqueness is decided when a lineup is
read, whatever the reader. State the current rule only; do not narrate that it
used to be checked later.

## Gate

`invoke pre` clean, `python -m pytest` green.

## Handover

**Done: all five work-order steps.**

1. Repro kept in a new `tests/test_lineup_parser.py`. Verified it fails on the
   pre-change `src/`: the duplicate-name case raised the *merge-result* clash
   message instead of the read-time one, and the duplicate-alias case did not
   raise at all — the sync would have written the file. Both pass now. The file
   also holds the two parser unit tests moved out of `test_upstream.py`.
2. `parse_lineup(data)` added to `standalone/registry.py`, returning configs and
   `[keys]` metadata. Body is the old `Registry.load()` plus `key_info`, with
   `upstream.configs_from_data`'s `isinstance(entry, dict)` guard kept — the
   registry path did not have it and would have raised `AttributeError` on a
   malformed section.
3. `Registry.load()` is `parse_lineup(_read_data(...))[0]`. `key_info()` reads
   the `[keys]` table through a private helper that `parse_lineup` also calls,
   so it does not re-validate the entries.
4. `configs_from_data` / `key_infos_from_data` deleted; all six call sites take
   `parse_lineup`. In `sync_file` the keys now come from the same parse as the
   configs instead of a second pass over the same dict.
5. Confirmed: `_attempt_sync` catches `(ValueError, OSError)`, so a broken
   curated preset stays a caught refresh failure.

**Decided during implementation, not in the plan:**

- `_check_render_faithful` now catches `ValueError` rather than only
  `TOMLDecodeError`. Its `parse_lineup` call can now fail validation, not just
  decoding, and that must stay inside the "nothing written" contract — one
  existing test (a source carrying a `[[custom]]` the target also has, rendered
  twice) reaches this exact path and previously reported the wrong thing. The
  message reads "would not read back" instead of "would not parse".
- `_check_name_clash` on the merge result is **kept**. It is not the same check:
  a clash can be created *by the merge* between two individually valid files,
  which is what `test_sync_file_leaves_the_target_untouched_on_a_clash` covers,
  and its message tells the user which entry to rename.
- Spec update landed in `rules/direct-aliases.md`, not `sync-merge.md`.
  `sync-merge.md` never stated a uniqueness rule; the rule lives in
  `direct-aliases.md` ("A name identifies exactly one entry"), which claimed
  it was enforced at two named sites. Corrected there to state that uniqueness
  of names and aliases is decided when a lineup is read, whoever reads it —
  written in one place, per the standing rule.

**Left out:** nothing.

## Fix round, after review

Four findings changed runtime behavior and were fixed; the rest were spec and
style corrections in the same diff.

- **The alias-collision guidance was shadowed.** Routing the sync path through
  the read-time check meant two alias-following entries that a catalog move
  lands on one name got the generic duplicate-name message instead of the one
  that says a rename will not stick. The guidance is now one shared constant in
  `standalone/registry.py`, appended by the read-time check when either
  colliding entry carries an alias, and used verbatim by the merge-result check
  — which is still reachable for a clash the merge itself creates between two
  individually valid lineups, and has its own test.
- **A malformed entry is refused, not skipped.** The `isinstance` guard carried
  over from the sync path made `Registry.load()` silently drop a non-table entry
  where it used to raise — reachable through a `.json` registry, and a silently
  smaller pool is exactly the failure the invariants exist to prevent. It now
  raises, naming the section and position, and keeps `ValueError` on purpose
  (`# noqa: TRY004`): a background refresh catches that type, and a `TypeError`
  would escape it.
- **The rendering-mismatch guard lost its test.** Measured with `coverage`: the
  existing test used to reach the "would carry X instead of Y" branch and, after
  the change, stopped one branch earlier on the duplicate check, leaving live
  code uncovered. It has a direct unit test now.
- **A malformed `[keys]` table crashed the writer.** The parser tolerated a
  non-table `[keys]` by returning nothing, while the file writer reached past it
  into the raw parsed data and died on it with a `TypeError` — outside the error
  type either the CLI or a background refresh catches, so a plausible typo
  surfaced as a traceback or as a logged library bug. The same divergence the
  plan set out to close, one section over. `[keys]` is now refused by the parser
  like any other malformed section, which also puts the failure before the
  writer is ever reached — so the writer needed no change, and the code the next
  plan deletes was left alone. The `env` command, which reads that table to tell
  a human how to obtain a key, reports a bad lineup the way it already reports
  every other bad argument instead of raising through.

Not fixed, deliberately: `Registry.load()` and `Registry.key_info()` still read
the file twice, so `load_sync_source` parses it twice. The registry protocol is
two methods, and the only ways to collapse them are caching a parse — which
changes what a second call means when the file has been edited — or breaking the
protocol. Neither belongs in this plan; the double *validation* the plan
targeted is gone.

Spec corrections in the same round: `rules/direct-aliases.md` now says
uniqueness is decided when a lineup *file* is read (the DB registry leans on its
primary key for names, so "every registry" would have overclaimed), names the
merge-result check as the separate thing it is, and states that an alias entry's
fix is never a rename. `rules/sync-merge.md`'s description of the pre-write
guard says "read back as a lineup" rather than "parsed", which is what it now
does.

**Gate:** `invoke pre` clean (0 pyrefly errors, all hooks passed);
`python -m pytest` → 1212 passed, 0 skipped, 0 errors, Docker up.

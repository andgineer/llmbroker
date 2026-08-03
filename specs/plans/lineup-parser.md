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

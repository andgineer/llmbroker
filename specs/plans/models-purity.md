# models.py is not what it says it is

**Skeleton — not ready to implement.** Written in full after
`lineup-file-ownership` merges, which already removes one of the four problems
below. Findings are against today's code and stay valid; the route does not.

## Goal

`models.py` opens with *"Pure data and the one cross-cutting capability
protocol. No I/O, no driver imports — safe to import from anywhere."* Four
things in it are none of those. Make the docstring true.

## Findings (evidence as of today, 515 lines)

1. **It logs.** `logger` is created at line 16. `_weight_from_metadata` (52-63)
   emits two `logger.warning` calls while parsing a stored row. A module every
   other module imports pulls in logging configuration and emits during a
   dataclass construction.

2. **It formats user-facing English.** `SyncReport.__str__`, `_kept_line` and
   `_retired_lines` (211-272) are ~60 lines of prose, including grammatical
   number agreement — `("it", "stays")` vs `("they", "stay")`. Two consumers:
   `print` in the CLI, `logger.info` in the broker.

3. **It holds the validators.** `check_limit`, `check_score`, `check_weight`,
   `check_unique_aliases`, `to_utc`, `with_utc_timestamps`. These are policy,
   not data, and several are imported by backends that want nothing else from
   this module.

4. **It holds `key_hash`.** A sha256 digest — the quota-scope identity used by
   shared cooldowns and dead-key drops. Belongs with the journal.

## A fifth, separate finding

Weight parsing exists twice with **deliberately different** policies:

| site | on a bad value |
|---|---|
| `models.py::_weight_from_metadata` | clamp and warn — a malformed row in a shared DB must not stop a broker starting |
| `standalone/registry.py::_weight_from_entry` | raise — a human wrote it and is looking |

The split is correct and both docstrings explain it. What is missing is that
neither names the other, so the next reader meets one copy and takes it for the
rule. Whatever the eventual shape, the two must reference each other or sit
adjacent.

## What changes before this plan is written

`lineup-file-ownership` step 7 already moves `SyncReport.__str__` out to a
renderer beside its two consumers, and replaces the alias notices with typed
facts. So finding 2 will be closed by then — **check before scoping**, and if
it was closed, say so rather than re-doing it.

`registry-ownership` adds a field to `LLMConfig` and rewrites the metadata
round-trip around it, so every line number and the 515-line count above are
stale by the time this is scoped — re-measure rather than trusting them. Check
the fifth finding against it too: it touches `from_metadata`, next door to the
weight parsing, and may have closed or moved that split already.

`model-list-vocabulary` rewrites the user-facing prose this module emits, so
whatever survives of finding 2 is text that has *already been corrected*. When
relocating it, move it verbatim: re-typing a report line from what is written
here would reintroduce the word that plan removed.

## Open questions for the real plan

- Do the validators go to one `checks.py`, or to the module that owns each
  rule (`check_score` with the optimizer, `check_limit` with the journal)? The
  second is more cohesive and more imports.
- `PoolSnapshot` implements `Mapping` — is that data or behavior? It is the
  host-facing read surface, so probably it stays.
- Does `_weight_from_metadata` keep logging after the move, or return a
  parse result its caller reports? The second removes the last import of
  `logging` from the data layer.

## Spec updates

None expected. Which module holds a validator is implementation, and a code
identifier in a spec has to earn one of the three exemptions.

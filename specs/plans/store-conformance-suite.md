# One conformance suite for every store

## Goal

Give the store layer the same shape the driver layer already has: one file that
says what *any* store must do, run over every store, and a second file holding
only what is true of the file layout alone.

## Why

`tests/test_driver_conformance.py` opens with "Behavior is tested once here —
rather than duplicated per backend", and that decision has held: it covers five
driver implementations from one file.

The store layer never got it. `FileStore` is tested in `tests/test_store.py`,
`DriverStore` in `tests/test_store_backends.py` — but the `queryable_store`
fixture those backend tests use *already includes the file store*. So a behavior
required of every store currently has two homes, and which one it lands in is a
coin flip. Ten such behaviors are written twice today.

**This is organization, not a coverage fix, and the plan should not be sold as
one.** Both claims that would have made it a bug fix were checked and refuted:

- A field-by-field mutation over the journal record (drop one field on the write
  side, run the suite) leaves no survivors — the lossless-persistence guarantee
  already holds on all four stores.
- The one asymmetry that looked real — "seeding the admin verdict map never
  overwrites an existing value" being tested for the file store only — is caught
  when broken on `DriverStore` too, indirectly, by the rebuild tests.

What it buys is that the *next* store behavior has one obvious home and lands on
four stores instead of one. What it costs is a large, purely mechanical diff
across two test files. Take it when that trade reads as worth it, not before.

## Current duplication

Universal behavior with a file-store-only copy. Left column is the copy to
delete; right column already runs on all four stores.

| `tests/test_store.py` | covered by |
|---|---|
| `test_file_store_rejects_naive_ts_on_record` | `test_record_rejects_naive_ts` |
| `test_file_store_record_normalizes_offset_to_utc` | `test_record_normalizes_ts_offset_to_utc` |
| `test_file_store_rejects_naive_since` | `test_calls_rejects_naive_since` |
| `test_file_store_since_is_inclusive_at_the_bound` | `test_calls_since_is_inclusive_at_the_bound` |
| `test_file_store_calls_respects_limit` | `test_calls_respects_limit` |
| `test_file_store_calls_scope_filter` | `test_calls_scope_filter` |
| `test_file_store_retention_purge_is_debounced` | `test_retention_purge_is_debounced` |

Genuinely file-specific — these stay, and are the reason `test_store.py`
continues to exist:

- day file named by the record's UTC date, not its own offset
- `since` skipping a whole expired day file without reading it
- a `since` whose local date leads its UTC date
- splitting across days, appending within a day, newest-first across day files
- purge unlinking whole expired day files
- the JSONL line shape, and the YAML disabled-map file
- every `InMemoryStore` test

Two entries need a judgment call rather than a rule: the retention purge and the
quality-record write exist in both files because each checks a *different* thing
— the universal behavior in one, the file mechanism in the other. Keep both,
renaming the file-store copy to say it is about the file mechanism.

## Work order

1. **Rename `tests/test_store_backends.py` → `tests/test_store_conformance.py`**
   and give it the driver suite's framing docstring: the behavior required of
   any store, tested once, over the file/sqlite/postgres/mongodb fixture.
2. **Delete the seven duplicated tests** from `tests/test_store.py`.
3. **Rename the two overlapping file-store tests** so their subject is the file
   mechanism, not the behavior: the day-file unlink and the JSONL line shape.
4. **Retitle `tests/test_store.py`** to what it now is — the file layout and
   `InMemoryStore` — and check no import is left unused.
5. Optional, only if step 2 leaves it obvious: move the `queryable_store`
   fixture's docstring into the renamed suite's module docstring, so the fixture
   is not the only place the four-store scope is written down.

## Verification

The mutation harness is the acceptance check, because a mechanical test move is
exactly the change that can silently delete coverage while staying green:

- Re-run the per-field journal mutation. Before and after must both report zero
  survivors across every journal field.
- Re-run the admin-seed mutation (make seeding overwrite an existing verdict).
  It must still be caught.
- Test count must drop by exactly the number of deleted duplicates, and by
  nothing else.

## Not in scope

- **Merging the store suite into the driver suite.** They test different layers:
  drivers move rows, stores move journal records. One file for both would need a
  fixture that is two things at once.
- **The `InMemoryStore` tests.** It implements the minimal contract, not the
  queryable one, so it cannot join a suite built on the queryable fixture.
- **Adding behaviors.** Anything that is not a move, a delete or a rename here
  belongs to a different plan.

## Spec updates

None. Where a test lives is not spec-worthy, and the rule it protects —
invariant 8 on lossless persistence — is already written.

## Gate

`invoke pre` clean, `python -m pytest` green. Docker must be running for the
testcontainer backends.

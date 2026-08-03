# One retention policy, one quality record

## Goal

Retention and the quality-record shape are implemented twice and their
constants declared five times. Collapse both to one definition each.

## Why

`_DEFAULT_RETENTION = timedelta(days=90)` appears in `standalone/store.py`,
`backends/ports.py`, `sqlite/store.py`, `postgres/store.py` and
`mongodb/store.py`. Changing the retention horizon today means finding five
files, and missing one is silent: the backend keeps its own horizon and nothing
fails.

## Current duplication

| concern | copy A | copy B |
|---|---|---|
| retention constant | `standalone/store.py` | `backends/ports.py` + 3 backend wrappers |
| purge debounce (`_PURGE_INTERVAL_SECONDS`, `_last_purge`, `_maybe_purge`) | `standalone/store.py` | `backends/ports.py` |
| quality-`Call` construction | `standalone/store.py::_new_quality_call` | `backends/ports.py::DriverStore.record_quality` |

## Work order

1. **New `src/llmbroker/journal_policy.py`** (pure, no I/O):
   - `RETENTION_DEFAULT: timedelta` — 90 days.
   - `PURGE_INTERVAL: float` — 3600.0 seconds.
   - `quality_call(llm_name, operation, score, *, call_id, scope) -> Call` —
     the one place a `kind="quality"` record is built. Both stores call it, so
     the record's shape cannot drift between backends.
   - `class PurgeClock` — holds `_last` and exposes `due() -> bool`. Only the
     debounce; the purge itself stays with each store, since one unlinks day
     files and the other issues a delete. A mixin owning both would force a
     shared `_purge` signature for no gain.

2. **`standalone/store.py`** — delete the two constants and `_new_quality_call`;
   `FileStore` takes `retention: timedelta = RETENTION_DEFAULT` and uses
   `PurgeClock`.

3. **`backends/ports.py`** — same for `DriverStore`; `record_quality` becomes a
   call to `quality_call` plus `self.record(...)`.

4. **`sqlite/store.py`, `postgres/store.py`, `mongodb/store.py`** — delete the
   local `_DEFAULT_RETENTION` from each; import `RETENTION_DEFAULT` for the
   default argument. These wrappers stay: they are the short import surface
   (`llmbroker.sqlite.Store`) the package's `__init__` rules exist to provide.

## Not in scope

Collapsing the per-backend `Store`/`Registry`/`Secrets` wrappers into one
generic factory. They are three files of ~15 lines per backend and they are the
documented ergonomic surface; merging them would trade a real import path for a
saved dozen lines. Adding a backend must stay easy, and these files are the
easy part.

## Tests

- `tests/test_store.py` and `tests/test_driver_conformance.py` pass unchanged.
- Add one test asserting `FileStore` and `DriverStore` produce quality records
  with identical field population for the same arguments — the drift this plan
  removes, made visible.

## Spec updates

None. Retention is already stated in `rules/journal.md` as a policy; where the
constant lives is implementation.

## Gate

`invoke pre` clean, `python -m pytest` green. Docker must be running for the
testcontainer backends.

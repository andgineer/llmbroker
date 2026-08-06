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

## Handover

**Done as written.** All four work-order steps implemented; no deviations from
the route.

- New `src/llmbroker/journal_policy.py` holds `RETENTION_DEFAULT`,
  `PURGE_INTERVAL`, `quality_call(...)` and `PurgeClock`.
- `standalone/store.py`: the two constants and `_new_quality_call` are gone;
  `FileStore` defaults to `RETENTION_DEFAULT` and owns a `PurgeClock`.
- `backends/ports.py`: same; `record_quality` is now one `quality_call` +
  `self.record`. Its `uuid`/`time` imports went with it.
- `sqlite/store.py`, `postgres/store.py`, `mongodb/store.py`: local
  `_DEFAULT_RETENTION` deleted, `RETENTION_DEFAULT` imported for the default
  argument. The wrappers themselves stay, as the plan says.

**Decisions the plan did not make.**

- `PurgeClock.due()` both tests and stamps the clock, so the two stores keep the
  exact "mark first, then purge" order they had. It takes no arguments: an
  interval override was written first and removed in review, since nothing set
  it and the debounce window is a single policy value, not a per-store knob.
- `_last_purge` is gone as an attribute, so
  `tests/test_store_backends.py::test_retention_purges_old_calls_via_maybe_purge`
  now forces the debounce open by assigning a fresh `PurgeClock()` instead of
  poking `float("-inf")`. No new production API was added for the test.

**Tests.** `test_store.py` and `test_driver_conformance.py` pass unchanged. The
new drift test —
`test_store_backends.py::test_file_and_driver_quality_records_are_identical` —
records the same quality arguments into a `FileStore` and a sqlite `Store`,
reads both back and asserts the records are equal once `id` and `ts` (a fresh
uuid and a fresh `now()` in both) are normalized away.

**Left out.** Nothing. No spec changes were needed, as the plan states:
retention is already a policy in `rules/journal.md` and the constant's home is
implementation.

**Gate.** `invoke pre` clean (0 pyrefly errors, all hooks passed);
`python -m pytest` → **1199 passed**, zero skips, zero errors, with Docker up
for the postgres/mongodb/localstack/vault testcontainers.

## Addendum: lossless persistence (came out of this plan's review)

Reviewing the plan's own drift test surfaced a gap wider than the test: nothing
asserted that a journal row survives its store intact. A mutation run over every
journal field — dropping one at a time on the write side of both stores — found
four that could vanish with the whole suite green. Two of them are evidence the
tail re-derives (how long the model took, and that it missed the caller's
budget), so losing them degrades selection silently; one is a host-facing
correlation id; one is the provider's error text.

No live defect: all four persist correctly today on all four stores. They were
simply unguarded, and the newest journal field was among them — the "remember to
write a test" mechanism had already failed once.

**Invariant 8 extended**, rather than a 22nd invariant added: the existing entry
already required the evidence to be *on a row* and stopped there. That a row must
then *survive its store whole* is the undelivered half of the same rule, and the
list is capped.

**One test replaces the plan's drift test.** The plan asked for a test showing
FileStore and DriverStore build a quality record identically. With a single
shared builder that drift is structurally impossible, so the test could only fail
on a serializer defect — a real property, but not the stated one, and checked on
two stores out of four. It is superseded by a round-trip test over the shared
backend fixture: every journal field populated with a distinguishable non-empty
value, read back, compared whole, on all four stores. Cross-store agreement now
follows from each store being lossless.

The field list is taken from the record type itself, so a new journal field
fails the test until it is given a sample value, and a sample value may not be
empty. Both guards were verified by forcing each condition.

Gate after this addendum: `invoke pre` clean, `python -m pytest` → **1202 passed**.

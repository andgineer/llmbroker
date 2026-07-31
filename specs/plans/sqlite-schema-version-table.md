# SQLite: keep the schema version in an `llmbroker_` table, not `PRAGMA user_version`

**Source of truth: https://github.com/andgineer/llmbroker/issues/12** — the deliverable is the
functionality described there. This plan is the suggested route; if the code has drifted from what
the plan assumes, the issue wins.

## Context

`sqlite/driver.py` stamps and reads its schema version in `PRAGMA user_version` (`_apply_ddl`,
:65-90). That value lives in the SQLite file header: one 32-bit integer for the whole database,
outside any table and outside llmbroker's `llmbroker_*` namespace. In the documented embedded
usage — `sqlite://<the host's own db>` — llmbroker claims a slot that belongs to the file, so it
collides with any host or library that also uses it.

The other two backends already keep the marker in their own namespace: `postgres/driver.py` uses
an `llmbroker_schema_version` table (DDL :28, upsert :36, read :77), `mongodb/driver.py` an
`llmbroker_schema_version` document (:64, :87). SQLite is the outlier.

The concrete failure (issue #12): a host that drops the `llmbroker_*` tables to recover from a
version mismatch cannot drop the header value, so the mismatch survives the only remedy the error
message offers, and recovery needs a manual `PRAGMA user_version` reset.

llmbroker does not migrate schemas in place — `ensure_schema` fails fast by design. This plan does
not change that; it changes only where the marker lives and how a legacy marker is interpreted.

## 1. The marker table

- Create `llmbroker_schema_version` in `sqlite/driver.py`'s DDL, mirroring the Postgres backend's
  single-row table and upsert.
- Keep it **out of `backends/spec.py`'s `TABLES`**, exactly as Postgres does: it is backend
  bookkeeping, not a store the generic layer reads. `SCHEMA_VERSION` therefore does not change and
  `tests/test_driver_conformance.py` needs no new expectations.

## 2. Resolution order in `ensure_schema` / `_apply_ddl`

The rule that makes "drop the tables and restart" actually work — and that never touches a value
that may belong to the host:

1. **`llmbroker_schema_version` exists** → its value is authoritative. `PRAGMA user_version` is
   neither read nor written, now or ever after.
2. **No marker table, but `llmbroker_*` tables exist** → this is a database written by a release
   that stamped the header, so read `PRAGMA user_version` as llmbroker's own legacy marker:
   - equal to `SCHEMA_VERSION` → adopt it: create the marker table stamped with that version (the
     DDL is `IF NOT EXISTS`-shaped, so the existing tables are untouched) and reset
     `PRAGMA user_version = 0`, handing the header back to the host;
   - anything else → raise the version mismatch. The advice in the message now holds: dropping the
     `llmbroker_*` tables lands the next start in case 3.
3. **No marker table and no `llmbroker_*` tables** → a fresh install. Create the schema and the
   marker table. **Do not read and do not reset `PRAGMA user_version`** — with no llmbroker tables
   in the file, a non-zero header value is far more likely to be the host's own (yoyo, alembic, a
   hand-rolled migration counter) than llmbroker's leftover, and clearing it would break the host
   the same way llmbroker's own claim did.

Case 3 is what fixes the reported breakage: after `DROP TABLE llmbroker_*` the next start sees no
llmbroker tables, ignores the stale header entirely, and self-heals.

`_schema_ready` (the per-path memo in `ensure_schema`, :115-131) keeps its current role; the
resolution above runs inside the same guarded section, once per database path — inside the
existing `BEGIN IMMEDIATE` transaction, so the header read, the marker write and the header reset
either all land or none do.

## 3. Error type

The mismatch in case 2 raises `SchemaVersionError` (`llmbroker.exceptions`), with the found and
expected versions attached — the type already exists and every driver already raises it. Reuse it;
do not invent a second type here.

## 4. Tests (`tests/test_schema_migration.py`)

Existing cases stay; new ones, all SQLite-specific:

- fresh database → marker table created and stamped, `PRAGMA user_version` still `0`;
- legacy database whose header equals `SCHEMA_VERSION` with llmbroker tables present → marker
  table created with that version, header reset to `0`, subsequent opens take path 1;
- legacy database whose header is an older llmbroker version with tables present → version
  mismatch raised;
- **the issue's regression case**: header left at an old version, `llmbroker_*` tables dropped →
  the next `ensure_schema` succeeds, creates the marker table, and leaves the header untouched;
- **the host-collision case**: a database with no llmbroker tables and a host-set
  `PRAGMA user_version` (e.g. `42`) → llmbroker creates its schema and the header still reads
  `42` afterwards. This is the property that keeps the fix from becoming the same bug in reverse;
- reopening a marker-table database never reads or writes the header (assert the header value is
  unchanged across an open that stamps the marker table).

## 5. Specs and docs

- `specs/reference/decisions.md` — state it as a rule, current state only: every backend keeps its
  schema marker inside its own `llmbroker_`-namespaced object, so dropping the `llmbroker_*`
  objects fully resets llmbroker's state; the SQLite file header belongs to the embedding
  application.
- `specs/reference/architecture.md` — if it describes the SQLite backend's versioning, bring it in
  line.
- `docs/src/en/` — wherever the embedded-SQLite setup is documented, note that the host keeps
  `PRAGMA user_version` for itself.

## Work order and done gate

1. Marker table DDL + read/upsert (§1).
2. Resolution order, including the two "do not touch the header" paths (§2).
3. Tests (§4) — write the regression and host-collision cases first; they are the reason for the
   change.
4. Specs and docs (§5).
5. Gate after every batch: `invoke pre` → no ruff/pyrefly errors, `python -m pytest` → `N passed`
   with zero skips. The version bump is the maintainer's (behaviour change for existing SQLite
   installs, no API break) — do not run `invoke ver-*`.

## Consumer follow-up (not part of this plan)

dinary carries `PRAGMA user_version = 0;` in `0002_drop_legacy_llmbroker_tables.sql` as the
workaround for exactly this bug; it can drop that line once this ships.

## Handover

Implemented in full: §1 marker table, §2 resolution order, §3 error type (reused
`SchemaVersionError`, unchanged message), §4 tests, §5 specs and docs. Nothing left out.

**What was built.** `sqlite/driver.py` gained `llmbroker_schema_version` (single row, `id = 1`
with a `CHECK`, mirroring Postgres) plus four small readers — `_has_table`, `_has_store_tables`,
`_marker_version`, `_header_version`. `_apply_ddl` now resolves the version before writing
anything and takes one of three paths exactly as §2 specifies. `PRAGMA user_version` is written
only on the adopt path, and only back to `0`. `_schema_ready`, the `BEGIN IMMEDIATE` section and
`SCHEMA_VERSION` (still 5) are untouched; `backends/spec.py` and the driver-conformance test did
not change.

**Decisions the plan did not make:**

- *Marker table present but its row missing* is treated as a fresh install (stamp it, do not read
  the header), not as a reason to fall back to the header. The plan's case 1 says a marker table
  makes the header off-limits "now or ever after", and this keeps that absolute. It also matches
  Postgres, where table-absent and row-absent are indistinguishable by construction.
- The stamp condition is *"the marker row was absent"*, not Postgres's `current == 0`. On the
  adopt path `current` equals `SCHEMA_VERSION`, so a literal copy of the Postgres condition would
  have created the marker table and left it empty.
- `_has_store_tables` matches with `GLOB 'llmbroker_*'`, not `LIKE 'llmbroker\_%'` — `_` is a
  single-character wildcard in `LIKE`. It also excludes the marker table by name, so the predicate
  reads "llmbroker *store* tables exist" on its own terms rather than only by call-site ordering.

**Deviation from §4.** The two pre-existing mismatch tests (`..._raises_on_version_mismatch`,
`..._is_still_a_runtime_error`) had to change: both built their stale database by setting the
header on an otherwise empty file, which is now the host-collision case and correctly does not
raise. They now corrupt the marker row instead, so they test case 1; the legacy-header mismatch
(case 2) is covered by the new `test_sqlite_legacy_header_of_older_version_raises`. "Existing
cases stay" holds for what they assert, not for how they set up.

Tests reach into `_schema_ready` through a `_ensure_schema` helper that pops the path first —
every new case reopens the same file after editing it behind the driver's back, which the
per-path memo would otherwise skip.

**Verification.** All seven SQLite cases were re-run against the pre-change driver (`git stash` of
`sqlite/driver.py` alone): 7 failed, 1 passed — the regression and host-collision cases fail
exactly as reported in issue #12.

**Gate:** `invoke pre` — all hooks passed, pyrefly 0 errors. `python -m pytest` — **819 passed**,
zero skips, zero errors (Docker up, testcontainer suites included).

Not done, by instruction: no version bump, no commit. `specs/plans/README.md` had its status line
updated to reflect this plan's state; its row stays until review and merge.
</content>

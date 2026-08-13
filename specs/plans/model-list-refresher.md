# Extract the lineup refresher from AsyncBroker

Written in full after `lineup-file-ownership` landed, against the code that plan
left. Ships in the same release as it.

## Goal

`AsyncBroker` is a routing façade with a lineup-refresh orchestrator living
inside it. Move the orchestrator out.

## Why

The class docstring names four collaborators — `Catalog`, `Router`, `PoolView`,
the learning hook. The fifth was never extracted: its own state (a clock, a
monotonic deadline, a background task, an on-disk stamp), its own lifecycle, and
its own failure policy (best-effort on the background path, raising on the
explicit call).

The mixing is already visible in the code: `ensure_pool` has to explain why part
of its work happens outside the provisioning lock, because a refresh re-enters
the catalog that the lock is protecting.

`lineup-file-ownership` already closed the part of this that was about the merge:
both targets now go through one merge site, `sync_file` is gone, and
`_present_refs` / `_keys_visible` / `_dead` / `_log_alias_lines` no longer exist
on the broker. What is left inside `AsyncBroker` is exactly the orchestration.

## What moves

Into a new `broker/refresher.py`, class `LineupRefresher`:

| from `AsyncBroker` | becomes |
|---|---|
| `_sync_source`, `_sync_interval`, `_sync_attempted`, `_next_refresh`, `_refresh_task`, `last_sync_report` | the refresher's own state |
| `_maybe_schedule_refresh` | `schedule()` |
| `_sync_on_start` | `before_provision()` |
| `_arm_refresh`, `_attempt_sync`, `_refresh_paid_catalog`, `_stamp_key`, `_target_identity`, `_follows_an_alias` | private, unchanged in behavior |
| `sync`, `_sync_file_target`, `_sync_registry_target`, `_log_alias_facts` | `sync()` and its two private target halves |

`AsyncBroker` keeps `sync()` as a one-line delegate and `last_sync_report` as a
read-only property — both are public surface and neither may change shape. It
also keeps the refresh-task cancellation call in `aclose()`, delegated to
`LineupRefresher.aclose()`, in the same position it is in now (see below).

## The seams

- **The refresher receives the `Catalog`, it does not own one.** It calls
  `resync`, `invalidate_declared`, `seed_secrets` and `apply`, but `ensure_pool`
  drives `provision` and the learner holds `resync` too, so the catalog is a
  collaborator shared by three objects and built by the façade — as it is today.
- **Whether the pool is live is a predicate, not a flag the refresher owns.**
  `sync()` reconciles the running pool only when the pool has been provisioned;
  the refresher takes a zero-argument callable for that and never writes it.
- **The clock stays keyed by (lineup, target).** Unchanged from today, and
  `lineup-refresh.md` states it: two projects on one machine have two lineups to
  keep current. The refresher therefore needs the registry (for a file
  registry's resolved path), the `home` directory, and the source label the
  broker resolved a string source to.
- **`declared` is passed in.** The refresher reads it for two decisions the
  paid-catalog clock depends on: whether anything follows an alias at all, and
  which aliases to ask the catalog about during a sync. Nothing else about
  `direct=` moves — resolving it stays the façade's overlay hook.
- **The constructor takes the ports plus that configuration**, which is over
  ruff's argument limit; one `# noqa: PLR0913` on the constructor that assembles
  a subsystem is the honest form, as it is on `AsyncBroker.__init__`.

## Also in this plan

Two helpers that are not the façade's either, and are cheap to move while the
file is open:

- `_find_custom` (~30 lines of two-keyspace error messages) → `broker/catalog.py`
  as `find_custom`, beside `check_overlay`. The skeleton said "next to the entry
  model"; `models.py` is the wrong home — it holds no prose and `models-purity`
  is about to remove what prose is left there. `catalog.py` already owns the
  other lookup-contract check, and `Catalog.entries()` is what feeds this one.
- `_default_secrets`, `_default_store`, `_zero_config_ports` → `broker/source.py`,
  which already owns source dispatch. `_file_target_path` goes with them: it is
  the same kind of statement about what a file source can be.

## Work order

1. **`broker/refresher.py`.** Move the state and the methods above verbatim,
   renaming only `_sync_on_start` → `before_provision` and
   `_maybe_schedule_refresh` → `schedule`. Public surface of the class:
   `before_provision()`, `schedule()`, `sync(source)`, `aclose()`,
   `last_report`.
2. **`AsyncBroker`.** Build the refresher in `__init__` after the catalog;
   `ensure_pool` calls `before_provision()` inside the lock and `schedule()`
   outside it, exactly as now; `sync()` delegates; `last_sync_report` becomes a
   property; `aclose()` awaits `refresher.aclose()` first — **before** the ports,
   because a refresh in flight would otherwise write through a registry whose
   driver is closing.
3. **The two helper moves** above, updating importers (no re-export shims).
4. **Tests.** The state moved, so the tests that drive the clock move with it:
   `broker._refresh_task` → `broker._refresher._task`, `broker._next_refresh` →
   `broker._refresher._next_refresh`. Two tests patch `AsyncBroker.sync` to make
   the *background* path hang or refuse; they must patch `LineupRefresher.sync`,
   since the delegate is not what the refresh calls.

## Tests

- `test_preset_autorefresh.py` and `test_broker_sync_knob.py`: retarget the
  clock/task reads and the two `patch.object(AsyncBroker, "sync")` sites. No
  assertion about behavior changes — that is the point of the extraction.
- One new test that the extraction preserved the ordering `aclose()` depends on:
  a refresh in flight is cancelled before the registry is closed.
- `AsyncBroker.last_sync_report` is read-only now: one test that it reflects what
  the refresher recorded, which is what a host reads.

## Spec updates

`rules/lineup-refresh.md` states the two gates, the check record and the failure
policy already, and all three are unchanged by this plan. Verify it still
describes the code and do not name the new class in it — the file is about the
behavior, not about which object carries it. `mission.md` says nothing about the
mechanism and needs no edit.

## Gate

`invoke pre` clean, `python -m pytest` green. One batch: the move is not
separable from the call sites it moves away from.

---

## Handover

### What is done

The whole work order, in one batch, plus the two new tests. `broker/refresher.py`
holds `LineupRefresher` with the state and methods the table names; `AsyncBroker`
keeps `sync()` as a delegate, `last_sync_report` as a read-only property, and the
cancel-before-the-ports call in `aclose()`. `broker.py` lost ~200 lines and its
docstring now names the fifth collaborator.

Moved as planned: `_find_custom` → `catalog.py::find_custom` (beside
`check_overlay`, not into `models.py` — reasoning in the plan), and
`_default_secrets` / `_default_store` / `_zero_config_ports` / `_file_target_path`
→ `broker/source.py` as public names. No re-export shims; importers updated.

### Decisions taken during implementation

- **`live` is a callable, not a flag.** `sync()` reconciles the pool only when it
  has been provisioned, and `_provisioned` stays the façade's. The refresher gets
  `lambda: self._provisioned` and never writes it.
- **The refresher receives the `Catalog`.** As the plan's seam section argues: the
  catalog is built by the façade and shared with the learner, so owning it here
  would be wrong.
- **`AsyncBroker` keeps `PresetSource`** and passes the same instance in — the
  declared-model overlay (`_resolve_declared`) resolves through it too, and it is
  immutable, so one object serves both.
- **No behavior changed.** Every assertion in the refresh tests is the one it was;
  what moved is where the tests reach for the clock and the task, and which class
  the two "make the background path hang/refuse" tests patch. Patching
  `AsyncBroker.sync` no longer affects the refresh — that was the one silent trap
  in the extraction, and it surfaced as a hang rather than a failure until the
  tests were retargeted.

### Deliberately left out

- `rules/lineup-refresh.md` is unchanged: it describes the two gates, the check
  record and the failure policy, all of which this plan preserves, and it names no
  class. Re-read against the code, still accurate.

### Gate

`invoke pre` clean (ruff, ruff-format, pyrefly: 0 errors), `python -m pytest`:
1233 passed, 0 skipped, 0 errors.

---

## Review round 1

Reviewed together with `lineup-file-ownership`, since they ship as one release.
**No defects in this plan's diff.** Both seams hold, both new tests the plan asked
for are present (`aclose` ordering, `last_sync_report` as a read-only property),
and every retargeted refresh test asserts what it asserted before. The findings
and their fixes are recorded in that plan's `## Review round 1` section; the only
one touching public surface reachable from here is the report renderer, now
`llmbroker.format_report`, which `last_sync_report` needs to be printable.

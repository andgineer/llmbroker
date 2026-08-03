# Extract the lineup refresher from AsyncBroker

**Skeleton — not ready to implement.** Written in full only after
`lineup-file-ownership` merges: that plan changes the seam this one extracts,
so line-level detail written now would be detail about code that no longer
exists. Ships in the same release as `lineup-file-ownership`.

## Goal

`AsyncBroker` is a routing façade with a lineup-refresh orchestrator living
inside it. Move the orchestrator out.

## Why

The class docstring names four collaborators — `Catalog`, `Router`, `PoolView`,
the learning hook. The fifth was never extracted, and it is roughly 360 of the
class's 930 lines: its own state (a clock, a monotonic deadline, a background
task, an on-disk stamp), its own lifecycle, and its own failure policy
(best-effort on the background path, raising on the explicit call).

The mixing is already visible in the code: `ensure_pool` has to explain why part
of its work happens outside the provisioning lock, because a refresh re-enters
the catalog that the lock is protecting.

## What moves

State: `_sync_source`, `_sync_interval`, `_sync_attempted`, `_next_refresh`,
`_refresh_task`, `last_sync_report`.

Behavior: `_arm_refresh`, `_maybe_schedule_refresh`, `_sync_on_start`,
`_attempt_sync`, `_refresh_paid_catalog`, `_stamp_key`, `_target_identity`,
`sync`, `_sync_file_target`, `_sync_registry_target`, `_present_refs`,
`_keys_visible`, `_dead`, `_log_alias_lines`.

`AsyncBroker` keeps `sync()` as a delegate and `last_sync_report` as a property
— both are public surface and neither may change shape.

## Also in this plan

Three helpers that are not the façade's either, and are cheap to move while the
file is open:

- `_find_custom` (~30 lines of two-keyspace error messages) → next to the entry
  model, with the alias contract it enforces.
- `_default_secrets`, `_default_store`, `_zero_config_ports` → `broker/source.py`,
  which already owns source dispatch.

## Open questions for the real plan

- Does the refresher own the `Catalog`, or receive it? It calls `resync`,
  `invalidate_declared`, `seed_secrets` and `apply` — four of Catalog's methods
  — which suggests owning, but `AsyncBroker.ensure_pool` also drives `Catalog`.
- Where does the refresh clock live once `lineup-file-ownership` has unified the
  two sync targets: one clock per source, or per target identity as today?
- Cancellation on `aclose` currently happens before the ports close, for a
  stated reason. Confirm the extracted object preserves that ordering.

## Spec updates

`rules/lineup-refresh.md` states the two gates and the check record already.
Verify it still describes the code; do not restate the object's name in it.

# Simplify learning, alerts, and seeding (Plan 2 of 3)

Prerequisite: `specs/plans/simplify-core.md` (Plan 1) is fully landed and green.
Follow-up: `specs/plans/simplify-storage.md` (Plan 3).
Mission context and open decisions: `specs/mission-cost.md`.

This plan **changes implementation mechanics, not the learning capability**:

- Learning stays **per (model, operation)** with the existing four-tier selection
  semantics. What changes is the machinery: sliding windows of raw scores and
  plain means replace decayed aggregates, Wilson bounds, and the bandit apparatus
  (exploration, floors, latency ranking, auto-retirement).
- Learned state is **derived from the call journal** instead of being a second
  storage subsystem. The journal already persists every rating
  (`record_quality` writes `quality_score` onto the call row) and is
  **append-only, so concurrent instances cannot clobber each other** — this
  replaces both the summaries/fold machinery and any snapshot merging. No
  last-writer-wins anywhere.
- Alerts shrink to flip events + statuses in `snapshot()` (pull model for the UI).
- `SeedPolicy` disappears: `seed=` always behaves like today's `SYNC`.
- Key onboarding metadata becomes a TOML passthrough.

Durability rule (user-approved): learning persists across process runs iff a
queryable telemetry backend is configured (sqlite is one line of setup);
otherwise windows live in process memory only.

## Rules for the implementer (read first)

- Lint/format/type-check only via `invoke pre` (never call ruff directly). Tests via
  `python -m pytest`. Both must be green after **every numbered step**.
- `pytest.ini` runs `--doctest-modules`: doctests in `src/` execute as tests.
- No in-function imports; no `from __future__ import annotations`; Python 3.11+.
- Never edit `src/llmbroker/__about__.py`; never bump the version.
- Never use `pytest.skip`/`importorskip`/`skipIf`.
- Never delete a test that reproduces a confirmed bug; port it to the new surface.
- Comments: 1–2 lines, non-obvious WHY only.

---

## 2.1 Replace the statistics engine with per-(model, operation) score windows

`optimizer.py` (407 lines) — same verdict surface, simpler internals.

**Keep (verbatim semantics):**

- `max_delay`, `backoff_factor`, `rl_fail_count` / `on_rate_limited` /
  `on_success` — cooldown/backoff bookkeeping the router reads.
- The benched set (`set_benched`/`clear_benched`).
- Dead-key handling: HTTP 401/403 → drop from pool + alert naming the
  `api_key_ref` (currently in `OptimizerTelemetry._drive_fsm`,
  `optimizer.py:348-356`).
- The alerts list (`alerts()` drain-on-read, `add_alert`) — trimmed in 2.4.
- The verdict API shape used by the pool: per-operation demotions and the global
  verdict — reimplemented on windows below.

**Delete:**

- The decayed-summary dicts `_ranking`/`_latency`/`_quality` and their math:
  `usable_rate`, `mean_latency_ms`, `wilson_bound`, `_z_score`, `NormalDist`,
  `_RANK_N`/`_D_RANK`, `transport_decay`/`quality_decay`, `to_profile`,
  `load_summaries`.
- Knobs of the deleted math: `quality_confidence`, `quality_effective_n`,
  `quality_margin`, `demotion_realert_interval`, `min_sample_count`,
  `usable_rate_floor`, `removal_rate_floor`, `exploration_fraction`,
  `background_operations`.
- `OptimizerPolicy`, `FirstAvailablePolicy`, the `SelectionPolicy` protocol and
  the `policy=` parameter of `pool.acquire` (selection is tier + order, 2.2).
- Automatic retirement (`should_retire`): chronic transport failure is already
  handled by exponential cooldowns up to `max_delay`; the dead-key drop is the
  only automatic removal left.
- `QualitySummary` in `models.py` once the last importer is gone (state-store
  uses die in Plan 3).

**New learning core (~60 lines):**

```python
@dataclass
class Optimizer:
    max_delay: float = 3600.0
    backoff_factor: float = 2.0
    quality_floor: float = 0.3
    quality_window: int = 30       # ratings kept per (model, operation)
    quality_min_count: int = 10    # verdicts need at least this many

    _scores: dict[tuple[str, str | None], deque[float]]  # deque(maxlen=quality_window)

    def record_quality(self, name: str, operation: str | None, score: float) -> None: ...
    def is_demoted(self, name: str, operation: str | None) -> bool:
        """len >= quality_min_count and mean(window) < quality_floor."""
    def demoted_operations(self, name: str) -> frozenset[str | None]: ...
    def is_globally_demoted(self, name: str) -> bool:
        """Every operation with >= quality_min_count ratings is demoted, and at
        least one such operation exists (same rule as today, mean instead of
        Wilson)."""
    def reset_quality(self, name: str) -> None: ...     # all operations; enable_llm
    def load_scores(self, scores: dict[tuple[str, str | None], list[float]]) -> None: ...
        # replace windows wholesale — used by the journal rebuild (2.3)
```

Demotion flip detection (for the alert): compare the per-op and global verdicts
before/after each `record_quality`/`load_scores`; on False→True emit one alert +
`logger.warning` (global flip also `logger.error`, as today); True→False logs
`info`. No standing re-alerts.

## 2.2 Selection: four tiers unchanged; simpler ranking within a tier

Tier semantics are **unchanged** from `optimizer.md` (normal / deprecated /
globally-demoted-model's-untried-operation / demoted-for-this-operation);
`pool.tier_of(name, operation)` keeps its logic (`pool.py:187-195`) but reads
verdicts straight from the optimizer (`is_demoted`/`is_globally_demoted`) instead
of mirror dicts — delete `update_demotions`, `_demoted_operations`,
`_globally_demoted`, `_refresh_demotions` pushes.

Within the best non-empty tier, ranking replaces the bandit machinery with one of
(**OPEN DECISION — see `specs/mission-cost.md`; both are one line in `acquire`**):

- **Option A — curated priority**: `min(candidates, key=lambda s: s.order)` where
  `_Slot.order` is the registry/preset index. Best model takes traffic until it
  cools; 429 never reaches the caller (failover retries the next model within the
  same request); cost is one extra roundtrip on the request that hits quota
  exhaustion.
- **Option B — round-robin (today's behavior)**: `min(..., key=lambda s: s.pick_seq)`
  from Plan 1. Spreads quota, fewer 429 events, lower average answer quality.

Implement the tier partition now and wire whichever option is confirmed; keep the
other reachable by that one-line change.

## 2.3 Learned state derived from the call journal (no second storage subsystem)

**Facts this builds on:** every call row already carries `llm_name`, `operation`,
`status`, `latency_ms`, `called_at`, and `record_quality` already writes
`quality_score` onto the row (`sqlite/schema.py:24-41`,
`sqlite/telemetry.py:97-106`). The journal is append-only — concurrent instances
add rows, never overwrite, so cluster consistency needs no folds and no merging.

**Mechanism:**

- **Own ratings apply instantly**: `record_quality` appends to the in-memory
  window (2.1) in the same call that writes the journal row.
- **Others' ratings arrive by rebuild**: when telemetry is queryable, re-derive
  all windows from the journal on a debounce (`_REBUILD_TTL = 60.0` seconds,
  checked on activity — no background task, an idle broker does zero reads).
  Rebuild = fetch the most recent rated calls (reuse
  `calls(limit=quality_rebuild_limit)`, default 300, filter
  `quality_score is not None` in Python — one query, portable), bucket by
  `(llm_name, operation)`, keep the newest `quality_window` per bucket, call
  `optimizer.load_scores`. At provision, run one rebuild as the warm start.
- **Non-queryable telemetry** (logging/Jsonl/None): windows are in-memory only —
  session-scoped learning, documented degradation. A TOML+sqlite-telemetry
  script learns across runs.
- **Forgetting** = `purge_calls` (already exists; purging old rows drops them
  from future rebuilds — semantically correct).

**What this deletes:** `_ProfileSync` and `_ProfileSyncTelemetry` entirely
(`broker/broker.py:60-311`), `seed_summary`/`apply_summary_delta`/`read_summaries`
call sites, `_REFRESH_TTL`/`_SNAPSHOT_DEBOUNCE`, `write_snapshot`/
`write_all_snapshots`/`reset_quality`'s state-store loop, `peek_call`. The state
store keeps **cooldowns only** (Plan 3 shrinks its implementations).

**`LLMProfile` shrinks to the bench latch** (`models.py`):

```python
@dataclass
class LLMProfile:
    benched: bool = False
    benched_since: datetime | None = None
    benched_reason: str | None = None
```

`write_profile` is called only from `disable_llm`/`enable_llm` — a human action,
one writer, no concurrency problem. Warm start reads profiles for latches only.
The file registry's sibling-JSON profile file keeps working for the latch.
`enable_llm` additionally calls `optimizer.reset_quality(name)`; note the reset is
per-instance and the journal still holds old ratings — to make the clean-trial
semantics hold across rebuilds, `enable_llm` records the reset moment and the
rebuild ignores this model's ratings older than it (one timestamp per name in the
profile: `quality_reset_at: datetime | None`).

**The one remaining telemetry wrapper** replaces the current two-wrapper chain in
`broker/broker.py`:

```python
class _LearningHook:
    """Telemetry wrapper: cooldown bookkeeping, dead-key drops, quality windows,
    debounced journal rebuild."""
    async def record(self, call): ...          # keep try/finally semantics (optimizer.py:276-283)
    async def record_quality(self, call_id, score): ...
    async def maybe_rebuild(self, *, force=False): ...
```

## 2.4 Alerts: flip events + statuses in `snapshot()`

Alert emissions shrink to exactly four flip events, un-debounced except (d):

- (a) dead key (401/403 drop) — names the `api_key_ref`;
- (b) demotion flip — per-operation or global (2.1);
- (c) seed refusing a `model` identity change (unchanged, `catalog.py:183-187`);
- (d) all keyed models cooling simultaneously — keep the single
  `_last_underprov_alert` timestamp inline (`broker.py:593-616`), 60s.

Delete: degraded-tier alerts (`pool._maybe_alert_degraded_tier`, the
`on_degraded_tier` callback and wiring), floor-drop alerts (die with
`OptimizerPolicy`), standing re-alerts, and the four debounce maps
(`_demotion_alert_times`, `_global_alert_times`, `_last_degraded_alert`,
`_last_floor_alert`).

For the UI (pull model): `LLMSnapshot` gains `status: LLMStatus` derived at
`snapshot()` time: `NO_KEY | AVAILABLE | COOLING | DEMOTED | DEPRECATED | BENCHED`
(first match, benched winning; DEMOTED here = globally demoted). The host polls
`snapshot()`; `alerts()` remains for hosts that want the event log.

## 2.5 `SeedPolicy` removal

`seed=` keeps exactly today's `SYNC` semantics (add new preset entries; update
operational fields of preset-origin entries; refuse model-identity changes with
an alert; deprecate absent preset-origin entries, lift on reappearance; never
touch `origin: user`; never delete; bootstrap missing secrets from env):

- Delete the `SeedPolicy` enum (`models.py`), the `seed_policy=` parameter from
  `AsyncBroker`/`Broker` and stack paths, and the `IF_EMPTY`/`ADD`/`MIRROR`
  branches in `catalog.apply_seed` (`catalog.py:137-155`) — `_apply_sync` becomes
  the only path, inlined into `apply_seed`.
- Add one public helper (~15 lines): `llmbroker.copy_registry(source, dest,
  user_id=None)` — reads `source.load()`, add-or-updates each config into `dest`.
  That plus manual `remove()` covers the exotic MIRROR/ADD use cases explicitly.
- Update `architecture.md`: replace the `SeedPolicy` table and policy text with
  the single seeding behavior + `copy_registry` (current state only, no history).

## 2.6 Key onboarding metadata: plain passthrough

Keep the `key_info()` optional capability, drop the taxonomy machinery: `KeyInfo`
becomes `help: str` plus a free-form `extra: dict[str, str]` passthrough of the
TOML section; delete `EffortLevel`/`ValueLevel` and the ordering logic;
`python -m llmbroker env <config>` prints keys in **file order** with their help
lines. Update the "Key acquisition help" section of `architecture.md`.

## 2.7 Spec rewrite

Replace `specs/reference/optimizer.md` wholesale with a short spec (target: one
to two screens) stating the current rules only:

- cooldown: trust `Retry-After`, streak-scale by `backoff_factor**consecutive_fails`,
  cap at `max_delay`, flat 60s default base; dead key (401/403) → immediate drop
  + alert;
- learning: per `(model, operation)` window of the last `quality_window` ratings;
  demoted ⟺ ≥ `quality_min_count` ratings and mean < `quality_floor`; global
  verdict and the four tiers as before; recovery via new ratings, last-resort
  traffic, or `enable_llm` (window reset with `quality_reset_at`);
- learned state is derived from the call journal: append-only, cluster-consistent
  by construction, rebuilt on an activity debounce; persistent iff telemetry is
  queryable; `purge_calls` is the forgetting mechanism; the registry profile
  holds only the manual bench latch;
- selection: best non-empty tier, then the confirmed within-tier order (2.2);
  parallel per-LLM up to `rate_limit.parallel`;
- alerts: the four flip events; pool health via `snapshot()` statuses.

Update `architecture.md` cross-references (optimizer bullet, seeding, key help,
state-store description).

## 2.8 Tests

| File | Action |
|---|---|
| `tests/test_optimizer.py` (1040) | rewrite small: window math per (name, op), per-op + global flips with alerts, reset semantics, backoff counters, dead-key drop |
| `tests/test_optimizer_integration.py` | rewrite: rating→demotion→tier selection end-to-end; journal rebuild round-trip (sqlite telemetry); **two brokers sharing one journal converge on the same verdicts**; `quality_reset_at` excludes pre-reset ratings |
| `tests/test_pool.py` | tier precedence incl. tiers 2/3; within-tier order (per the confirmed option); demoted-only pool still serves |
| `tests/test_catalog.py` | drop IF_EMPTY/ADD/MIRROR cases; keep all SYNC cases; add `copy_registry` |
| `tests/test_broker_bench.py` | keep; latch + `quality_reset_at` |
| `tests/test_cli_env.py`, `test_registry_keys.py` | passthrough/file-order assertions |
| `tests/test_broker.py` | four alert events; `snapshot()` statuses |

Delete tests of deleted mechanisms (exploration, floors, Wilson bands,
retirement, summaries folds, profile score snapshots) — they assert behavior that
no longer exists; the one sanctioned case of test deletion. Bug-repro tests tied
to surviving behavior are ported, never deleted.

---

## Step order

1. **2.1 + 2.2** windows + tier wiring (one step — they share the verdict seam)
2. **2.3** journal-derived rebuild + `_LearningHook` + profile shrink
3. **2.4** alerts + `snapshot()` statuses
4. **2.5** SeedPolicy removal
5. **2.6** key-info passthrough
6. **2.7** spec rewrite

**Plan gate:** `invoke pre` + full `python -m pytest` green, zero skips. Then
proceed to `specs/plans/simplify-storage.md`.

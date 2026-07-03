# Implementation simplification — same functionality, less machinery

Two-phase refactor. **Zero functional change**: every behavior in
`specs/reference/architecture.md` and `specs/reference/optimizer.md` is preserved
bit-for-bit. Public API (`AsyncBroker`/`Broker` constructors and methods, port
protocols except `StateStoreProtocol`, subpackage class names) stays intact.

Order matters: Phase 1 (core substrate) first, because it removes loop-bound state
that Phase 2's contracts would otherwise have to accommodate; Phase 2 (storage layer)
second, because the collapsed state-store contract defines the driver protocol.

Expected outcome: src ~6000 → ~3400 lines, tests ~7700 → ~5000, DB roundtrips per
call ~6 → ~2–3 (and still exactly 0 when idle).

Run `invoke pre` + `python -m pytest` after every numbered step. Never bump version.

---

## Phase 1 — core substrate

### 1.1 Replace the `asyncio.Queue` routing substrate with a slot table

**Problem.** `LLMPool` (`src/llmbroker/broker/pool.py`) models "at most one in-flight
request per LLM" as an `asyncio.Queue` slot per LLM. Since `SelectionPolicy` arrived,
`acquire()` drains the whole queue, partitions, ranks, picks one, re-enqueues the rest
(`pool.py:234-265`) — a queue used as a list. The queue drags along:

- `loop.call_later` re-enqueue timers on every cooldown (`pool.py:299,356`) — loop-bound
  state that forces `sync.py`'s cross-thread `Future` construction dance;
- the drain-and-refill dance in `set_benched` (`pool.py:127-144`);
- `_reenqueue_config` benched guards (`pool.py:277-281`);
- the stale-slot check in `Router.chat` (`router.py:86-88`) and the defensive keyless
  branch (`router.py:90-98`);
- `_queue_acquire`'s three wait modes via queue primitives (`pool.py:213-218`).

**Design.** One dict `self._slots: dict[str, _Slot]` where

```python
@dataclass
class _Slot:
    config: LLMConfig
    key: str | None = None
    in_flight: bool = False
    cooldown_until: datetime | None = None   # aware UTC; availability derived vs now
    fail_count: int = 0
    benched: bool = False
    deprecated: bool = False
    last_picked: float = 0.0                 # monotonic; round-robin tie order
```

plus one `asyncio.Condition` notified by `release()`, `cool_down()`, `add()`,
`clear_benched()`, `drop()`. No timers anywhere: cooldown expiry is
`cooldown_until <= now` evaluated at selection time (same derivation as
`models.reconcile`). `InMemoryState` (`broker/state.py`, 43 lines) folds into the
slot fields and is deleted; `pool.state(name)` builds `LLMState` from the slot.

- **Availability predicate**: `key is not None and not benched and not in_flight and
  (cooldown_until is None or cooldown_until <= now)`.
- **`acquire(wait, policy, operation)`**: loop — collect available slots; if any:
  tier-partition (`tier_of` unchanged), `policy.select` within the best tier (or
  round-robin by `min(last_picked)` when `policy is None`, preserving today's FIFO
  rotation), set `in_flight=True`, `last_picked=monotonic()`, fire the degraded-tier
  alert hook, return. If none: `wait==0` → raise; otherwise wait on the Condition
  with timeout `min(nearest cooldown expiry, remaining wait budget)`, then re-check.
  `wait=None` blocks indefinitely (Condition wakes on release/cooldown expiry timeout).
- **`release(config)`**: `in_flight=False`, notify. Ignore unknown names (slot removed
  mid-flight — replaces the router's stale-slot check).
- **`cool_down(config, delay)`**: set `cooldown_until`, `fail_count+=1`,
  `in_flight=False`, persist to state store (unchanged), notify. No `call_later`.
- **`apply_shared_cooling(config)`**: on a stored COOLING entry, copy
  `cooldown_until` / `max(fail_count)` into the slot, `in_flight=False`, return True.
  No timer. Cache logic (`_get_store_cache`, `_STORE_CACHE_TTL`) unchanged.
- **`set_benched(name)`**: set the flag; no queue drain. In-flight calls finish
  normally; the flag excludes the slot from every later selection.
- **`add(cfg, key)`**: upsert slot; the keyless→keyed transition needs no special
  casing anymore (availability is derived, nothing is enqueued).
- **`drop(name)`**: `del self._slots[name]` — the per-name marker cleanup loop
  (`pool.py:111-121`) collapses since markers live on the slot.

**Router** (`router.py`): delete the stale-slot check and the defensive keyless
branch (lines 86-98); keep the eager zero-keyed-configs check (line 74). Everything
else unchanged.

**Tests.** Existing pool/router behavior tests must pass unchanged. Tests that poke
queue internals get rewritten against the public surface. Add: benched-while-in-flight
(finishes, then excluded), cooldown expiry without event-loop timers (freeze/advance
time), `wait=` semantics (0 / finite / None), round-robin order with `policy=None`.

### 1.2 Simplify `sync.py` construction

With no loop-bound `asyncio.Queue` in `AsyncBroker.__init__` (Condition binds lazily
on first await — py3.11 semantics), the `Future`/`call_soon_threadsafe` build dance
(`sync.py:114-137`) collapses to a plain `AsyncBroker(...)` call on the caller
thread. The background loop thread stays (it runs the coroutines); `weakref.finalize`
lifecycle stays.

### 1.3 One `Debounce` helper

The same timestamp-map debounce is hand-rolled four times:
`_ProfileSync._demotion_alert_times` + `_global_alert_times` (`broker.py:87-88`),
`LLMPool._last_degraded_alert` (`pool.py:64`), `AsyncBroker._last_underprov_alert`
(`broker.py:414`), `OptimizerPolicy._last_floor_alert` (`optimizer.py:367`).

Add to `optimizer.py` (or a small `_util.py`):

```python
class Debounce:
    def __init__(self, interval: float) -> None: ...
    def ready(self, key: Hashable = None) -> bool:
        """True (and arms the window) if `interval` has passed since the last True."""
```

Replace all five maps/fields. Behavior identical, ~60 lines and four idioms → one.

### 1.4 Merge the telemetry wrapper chain

Per call the event flows through `_ProfileSyncTelemetry` → `OptimizerTelemetry` →
real telemetry — two wrapper classes with `__getattr__` passthroughs and a
`peek_call` back-channel (`broker.py:278-311`, `optimizer.py:262-332`). Merge into
one `OptimizerTelemetry(inner, optimizer, pool, profile_sync | None)` in
`optimizer.py`: `record()` = inner record → drive FSM → `profile_sync.on_call`;
`record_quality()` = resolve index → inner → aggregate → `profile_sync.on_quality`.
`peek_call` and `_ProfileSyncTelemetry` are deleted; `_ProfileSync` itself stays
(it owns warm-start/snapshot/refresh, which are not telemetry).

### 1.5 Spec touch-up

`architecture.md` "Core" bullet mentions the queue ("One `asyncio.Queue` slot per
LLM… cooldown via `loop.call_later` re-enqueue") — implementation detail in a spec.
Restate as behavior only: "at most one in-flight request per LLM; a cooling LLM is
skipped until its cooldown expires."

---

## Phase 2 — storage layer: one driver per DB, ports written once

### 2.0 Problem

Four domain ports × four DBs ≈ 16 implementations. Every method repeats the same
ritual (`check_user_id` → uid normalization → `ensure_schema` → acquire connection →
one statement → error/JSON translation); only the statement is DB-specific. Schema
DDL, migrations, and the `(user_id IS NOT DISTINCT FROM …)` scoping idiom are
re-written per DB. Tests multiply by the same matrix.

### 2.1 Collapse `StateStoreProtocol` to batched operations

New contract (in `protocols/state_store.py`) — same semantics, fewer/batched calls:

```python
class StateStoreProtocol(Protocol):
    async def read_all(self, user_id=None) -> StateSnapshot: ...
    async def apply(self, batch: StateBatch, user_id=None) -> None: ...
```

- `StateSnapshot` = `(states: dict[str, LLMState], summaries: dict[tuple[str, str|None, str], QualitySummary])`
  — replaces today's separate `read` + `read_summaries`; one roundtrip where the DB
  allows (postgres: two statements on one acquired connection; redis: one pipeline;
  mongo: two awaits on one client — acceptable; sqlite: local, free).
- `StateBatch` (a small dataclass) carries any of: cooldown writes
  `list[(name, LLMState)]`, summary deltas
  `list[(name, operation, kind, decay_pow, add_w, add_g, add_wsq, add_count)]`,
  and seeds `list[(name, operation, kind, QualitySummary)]` (insert-if-absent).
  One roundtrip: postgres — one transaction of executemany'd upserts; redis — one
  `WATCH`-CAS pipeline over the touched fields; mongo — one `bulk_write`; sqlite —
  one connection/transaction. Fold arithmetic per backend is unchanged from today
  (the arithmetic-UPSERT / CAS implementations are kept verbatim, just grouped).

Call-path effect (in `_ProfileSync.on_call` / `on_quality`, `broker.py:125-171`):
the 1–2 `apply_summary_delta` awaits become one `apply(batch)`; `warm_start`'s
per-(op,kind) `seed_summary` loop (`broker.py:100-108`) becomes one `apply` with
seeds; `reset_quality`'s per-row delta loop (`broker.py:258-273`) becomes one batch.
`_maybe_refresh` + `PoolView.snapshot` + `pool._get_store_cache` all read via one
`read_all` and can share the same 2s-TTL cache object (single reader path in
`_ProfileSync`, exposed to pool/view) — the two independent read paths merge.

Per-call cost after this step: 1 batched write + amortized 1 read (+1 telemetry
insert, +1 registry snapshot per 30s window). Idle: 0.

### 2.2 The `Driver` protocol and declarative schema

New subpackage `llmbroker/backends/`:

- `backends/spec.py` — declarative table specs (name, key columns, indexed columns,
  JSON payload columns), one shared definition of `llmbroker_registry`,
  `llmbroker_calls`, `llmbroker_secrets`, `llmbroker_state`, `llmbroker_summaries`,
  and the schema version. Each driver's `ensure_schema` renders DDL from this spec
  (sqlite/postgres) or creates collections/indexes (mongo); drop-based vs additive
  migration rules per table stay exactly as documented in `architecture.md`.
- `backends/driver.py` — the per-DB contract. Deliberately record-shaped, not
  domain-shaped:

```python
class Driver(Protocol):
    async def ensure_schema(self) -> None: ...
    # keyed records (registry rows, secrets, state docs)
    async def fetch(self, table: str, scope: Scope) -> list[Row]: ...
    async def get(self, table: str, key: Key, scope: Scope) -> Row | None: ...
    async def insert(self, table: str, key: Key, row: Row, scope: Scope) -> None: ...   # raises DuplicateKey
    async def upsert(self, table: str, key: Key, row: Row, scope: Scope) -> None: ...
    async def update(self, table: str, key: Key, fields: Row, scope: Scope) -> bool: ...
    async def delete(self, table: str, key: Key, scope: Scope) -> bool: ...
    # journal (llmbroker_calls)
    async def append(self, table: str, row: Row) -> None: ...
    async def recent(self, table: str, scope: Scope, limit: int) -> list[Row]: ...
    async def set_field(self, table: str, key: Key, field: str, value: object) -> bool: ...
    async def metrics_rows(self, table: str, scope: Scope, since: datetime | None) -> list[MetricsRow]: ...
    async def purge(self, table: str, before: datetime) -> int: ...
    # state store
    async def read_state(self, scope: Scope) -> tuple[list[Row], list[Row]]: ...
    async def apply_state(self, batch: StateBatch, scope: Scope) -> None: ...
    async def aclose(self) -> None: ...
```

`Scope` is the `user_id`; `Row` is a plain dict — all JSON/dataclass translation
lives above the driver. Each method body in a concrete driver is the one statement
that is genuinely DB-specific today. `check_user_id`, uid normalization, and lazy
`ensure_schema` gating move into the generic layer and are written once.

### 2.3 Generic ports (written once)

`backends/ports.py`: `StoreRegistry(driver)`, `StoreSecrets(driver, require_user_id=False)`,
`StoreTelemetry(driver)`, `StoreStateStore(driver)` — implement the existing
`MutableRegistryProtocol` / `MutableSecretsProtocol` / `QueryableTelemetryProtocol`
and the new `StateStoreProtocol`. They own: user-scope checks, `to_dict/from_dict`
round-trips (`LLMConfig.from_metadata`/`to_metadata`, `LLMProfile`, `LLMState`,
`Call`), `KeyError`/`ValueError` semantics (add = create-only, update = modify-only),
`reconcile()` on state reads, the never-overwrite rule for profile vs metadata
columns. **Port protocols other than the state store do not change** — the broker,
catalog, and `_ProfileSync` are untouched by this phase except the state-store
call sites from 2.1.

### 2.4 Concrete drivers

- `llmbroker/sqlite/driver.py` — aiosqlite; keeps the `BEGIN IMMEDIATE` cross-process
  migration and `PRAGMA user_version` tracking from `sqlite/schema.py` verbatim.
- `llmbroker/postgres/driver.py` — asyncpg pool; caller owns the pool, `aclose()`
  no-op (unchanged contract); keeps the arithmetic-UPSERT fold
  (`postgres/state_store.py:12-22`) inside `apply_state`.
- `llmbroker/mongodb/driver.py` — motor; explicit `user_id: None` in documents
  (unchanged), `bulk_write` for `apply_state`.
- **Redis stays a direct `StateStoreProtocol` implementation** (it serves one port
  only; a generic layer buys nothing there). It is rewritten to the new
  `read_all`/`apply` contract, keeping the existing WATCH/MULTI/EXEC CAS fold and
  hash layout (`redis/state_store.py`).

Subpackage facades keep every public name and constructor signature:
`llmbroker.sqlite.Registry(path)` → `StoreRegistry(SqliteDriver(path))`, etc. The
`stack=` sugar (`BackendStack`) now builds one driver and shares it across the four
ports — its current per-port construction simplifies.

Delete after ports are green: `{sqlite,postgres,mongodb}/{registry,secrets,telemetry,state_store}.py`
and the per-DB `schema.py` files (DDL moves to spec + driver).

### 2.5 Tests

- `tests/backends/test_driver_conformance.py` — one parametrized suite (sqlite file,
  sqlite `:memory:`, postgres testcontainer, mongodb testcontainer) exercising every
  `Driver` method: CRUD semantics, DuplicateKey, scoping exactness (scoped vs
  unscoped never mix), fold arithmetic (including the insert-if-absent identity
  `0*d+x == x`), migration from an empty DB and from the previous schema version,
  batch atomicity under two concurrent writers.
- Port behavior tests run once against an `InMemoryDriver` (new, trivial, also useful
  as a test double for users).
- The new-contract redis store keeps its own suite (fakeredis + testcontainer as today).
- Existing per-(port × DB) suites are deleted where the conformance suite covers
  them; keep one thin smoke test per subpackage (construct via facade, one write,
  one read) so import wiring stays covered.
- Repro tests for previously confirmed bugs are preserved and ported, never deleted.

### 2.6 Spec touch-up

`architecture.md`: the four-backend table and battery matrix stay (they describe
capability, which is unchanged). Add one sentence to "Where each kind lives":
dependency-carrying backends are implemented as a single storage driver per DB
behind shared port logic; implementing a custom backend means either one driver or
one full port. The columns-vs-JSON section and migration rules stay (they still
govern the driver schemas). Update the `StateStoreProtocol` description to the
batched read/apply shape.

---

## Step order and gates

1. 1.1 pool + router (largest risk — land alone, full test pass)
2. 1.2 sync.py
3. 1.3 Debounce
4. 1.4 telemetry chain merge
5. 1.5 spec touch-up; **gate: `invoke pre` + full pytest green**
6. 2.1 state-store contract + call-path batching (redis + existing three stores
   updated in place to the new contract; this step changes no file layout)
7. 2.2–2.4 driver + generic ports + facades, one DB at a time: sqlite → postgres →
   mongodb, deleting each superseded module only when its facade passes conformance
8. 2.5 test consolidation
9. 2.6 spec touch-up; final gate: `invoke pre` + full pytest, zero skips

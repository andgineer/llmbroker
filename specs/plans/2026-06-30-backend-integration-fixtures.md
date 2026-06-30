# Backend integration fixtures + broker E2E + optimizer gap-fill

## Goal

Three coupled deliverables:

1. **Reusable per-port and stack fixtures** in `tests/conftest.py` so integration tests can
   compose registry / secrets / state_store / telemetry over their real backends — including
   *heterogeneous* deployments (registry in Postgres, secrets external, state in Redis), not only
   the homogeneous diagonal.
2. **`tests/test_broker_integration.py`** — end-to-end `AsyncBroker` over curated realistic stacks,
   covering wiring + persistence across the backend boundary.
3. **Optimizer gap-fill** in the existing `tests/test_optimizer_integration.py` — close FSM/policy
   edge cases currently untested.

## Background: why only telemetry is parametrized today

`test_optimizer_integration.py` is an *optimizer* integration suite. Of the broker's four external
ports, the `Optimizer` touches only telemetry in its data path:

- FSM is driven purely by `Call` records flowing through `OptimizerTelemetry.record()` → `_drive_fsm()`.
- Warm-start reads `telemetry.metrics()`.
- `registry` / `secrets` / `state_store` are bypassed entirely — the tests build
  `LLMPool(state_store=None)` directly and call `pool.add(_cfg(...), "key")`.

So parametrizing registry/secrets/state there would execute no new optimizer code. The real gap is a
**broker-level** suite that wires a full stack and runs realistic routing/persistence — that is what
the new fixtures serve.

The four ports are **orthogonal axes**. Realistic deployments mix them (e.g. registry+telemetry in
one DB, secrets in Vault, state in Redis). The fixture design must therefore compose ports
independently, and the E2E suite must run over *curated representative stacks*, never the full
Cartesian product (reg×sec×state×tel ≈ hundreds of combos).

### Available backends per port (verified in source)

| Port | Available today | Future |
|---|---|---|
| registry | toml (standalone), sqlite, postgres, mongodb | — |
| secrets | env/Dict (standalone), sqlite, postgres, mongodb | Vault (issue #3) |
| state_store | None (no-persist), sqlite, postgres, mongodb, redis (fakeredis) | — |
| telemetry | NoTelemetry (toml), sqlite, postgres, mongodb | — |

Constructors are symmetric per backend:
- sqlite: `Registry(db_path)`, `Secrets(db_path)`, `StateStore(db_path)`, `Telemetry(db_path)`
- postgres: each takes `asyncpg.Pool`
- mongodb: each takes `AsyncIOMotorDatabase`
- redis: `StateStore(aioredis.Redis)` — tests use `fakeredis.aioredis.FakeRedis(decode_responses=True)`
- standalone: `Registry(toml_path)`, `Secrets()` (env) / `DictSecrets(mapping)`, `NoTelemetry()`

---

## Part A — conftest fixtures ✓ DONE

Two independent mechanisms. Keep existing `pg_pool`, `mongo_db`, `any_telemetry`,
`queryable_telemetry` fixtures untouched (optimizer suite still uses them).

### A1. Per-port fixtures (port-contract testing)

Each independently parametrized over its own backends. **Never combine two `any_*` on one test**
(that produces the Cartesian explosion). These are for testing one port's contract across backends
and, as a follow-up, for replacing the isolated `test_<backend>_<port>.py` files.

```python
@pytest.fixture(params=["toml", "sqlite", "postgres", "mongodb"])
async def any_registry(request, tmp_path_factory, pg_pool, mongo_db) -> MutableRegistryProtocol | RegistryProtocol: ...

@pytest.fixture(params=["env", "sqlite", "postgres", "mongodb"])
async def any_secrets(request, tmp_path_factory, pg_pool, mongo_db) -> SecretsProtocol: ...

@pytest.fixture(params=["sqlite", "postgres", "mongodb", "redis"])
async def any_state_store(request, tmp_path_factory, pg_pool, mongo_db) -> StateStoreProtocol: ...
```

- Reuse the session `pg_pool` / `mongo_db`. Docker auto-marking already keys off those names in
  `pytest_collection_modifyitems` — extend the predicate if needed so any fixture pulling them in
  gets `pytest.mark.docker`.
- Teardown truncates that port's table/collection (mirror the existing `any_telemetry` cleanup:
  `DELETE FROM llmbroker_<table>` for pg, `delete_many({})` for mongo, fresh `tmp_path` for sqlite,
  fresh `FakeRedis` for redis — fakeredis is per-instance so no shared state to clean).

### A2. `stack` factory (broker E2E)

A curated, *named* list of full stacks. The fixture yields a **factory** `make_broker(...)` (not a
prebuilt broker) so restart scenarios can build a second `AsyncBroker` over the *same* underlying
store.

```python
@dataclass
class Stack:
    name: str
    registry: RegistryProtocol
    secrets: SecretsProtocol
    state_store: StateStoreProtocol | None
    telemetry: TelemetryProtocol
    queryable: bool          # telemetry implements QueryableTelemetryProtocol
    persistent: bool         # state_store is not None

    def make_broker(self, **kw) -> AsyncBroker:
        return AsyncBroker(self.registry, secrets=self.secrets,
                           state_store=self.state_store, telemetry=self.telemetry, **kw)
```

Full stack list (chosen: all homogeneous + scaled + minimal):

| Stack name | registry | secrets | state_store | telemetry |
|---|---|---|---|---|
| `all_sqlite` | sqlite | sqlite | sqlite | sqlite |
| `all_postgres` | postgres | postgres | postgres | postgres |
| `all_mongodb` | mongodb | mongodb | mongodb | mongodb |
| `scaled` | postgres | DictSecrets(env-like) | redis (fakeredis) | postgres |
| `minimal` | toml | env (standalone Secrets) | None | NoTelemetry |

- `scaled` is the heterogeneous representative: registry+telemetry in Postgres, secrets external,
  state in Redis — the real "scaled deployment" shape. When Vault (#3) lands, add a `vault_stack`
  row swapping `DictSecrets` → Vault secrets; one line, no fixture change.
- Provide **two parametrizations** to honor the project's "no skip" rule for non-applicable pairs:
  - `stack` — all 5 stacks (for E1/E4/E5/E6).
  - `persistent_stack` — only stacks where `persistent and queryable` (excludes `minimal`; for
    E2/E3 which need queryable telemetry / a real state_store). Mirrors the existing
    `any_telemetry` vs `queryable_telemetry` split.
- Seeding: tests seed registry/secrets through the mutable protocols (`registry.add()`,
  `secrets.set()`); the `toml`/`env` standalone variants for `minimal` need a temp TOML file +
  env vars (use `monkeypatch.setenv`). Helper `_seed_stack(stack, configs, keys)` centralizes this.
- HTTP is mocked at `llmbroker.chat.httpx.AsyncClient` (reuse the `_http_ok` / `_http_error`
  helpers from `test_broker.py`, or lift them into conftest).
- Teardown truncates every backing table/collection used by the stack.

---

## Part B — tests/test_broker_integration.py ✓ DONE

E2E over the real stack. **Does not re-test the FSM** (that is the optimizer suite's job) — it
verifies port wiring + persistence across the backend boundary. Patch HTTP per scenario.

| # | Test | Param | Asserts |
|---|---|---|---|
| E1 | `test_provision_loads_registry_and_resolves_keys` | `stack` | after `ensure_pool`, pool has configs from registry and resolved keys from secrets |
| E2 | `test_warm_start_primes_delay_from_persisted_calls` | `persistent_stack` | pre-seed telemetry with RATE_LIMITED call → new broker's optimizer has `delay_for == max_delay` |
| E3 | `test_state_persists_across_restart` | `persistent_stack` | 429 cools an LLM → `state_store.write`; second broker on same store restores cooldown via `read` |
| E4 | `test_failover_routes_and_journals` | `stack` | llm1→429, llm2→OK: routes to llm2; queryable stacks expose the calls via `broker.calls()` |
| E5 | `test_all_offline_raises_and_alerts` | `stack` | all LLMs→429 → `NoLLMAvailableError` + under-provision alert from `broker.alerts()` |
| E6 | `test_catalog_mutation_persists` | `stack` | `broker.add()/remove()` writes through mutable registry; rebuilt broker sees the change |

- E4's journal assertion must branch on `stack.queryable` (NoTelemetry returns nothing); structure
  so the routing assertion runs everywhere and the `calls()` assertion only on queryable stacks
  (no `skip` — just an `if stack.queryable:` block, both paths assert something).
- E2/E3/E6 exercise the factory (`make_broker` called twice on one store).

---

## Part C — optimizer gap-fill (existing test_optimizer_integration.py) ✓ DONE

Pure FSM/policy tests over `any_telemetry` / `queryable_telemetry`. No new backend fixtures.

### C-A. Status-coverage extensions (cheap parametrization)
- **`UNAVAILABLE` → OFFLINE**: parametrize `test_rate_limit_drives_fsm_to_offline` over
  `[CallStatus.RATE_LIMITED, CallStatus.UNAVAILABLE]` — `_drive_fsm` treats them together.
- **ERROR 403**: parametrize `test_auth_failure_drops_llm_cleanly` over `http_status=[401, 403]`.

### C-B. Uncovered policy branches
- `test_policy_floor_drops_all_falls_back_and_alerts`: every candidate below `usable_rate_floor`
  → `select()` ranks over *all* candidates and emits one floor alert (respect `_FLOOR_ALERT_INTERVAL`).
- `test_policy_exploration_returns_random`: with `exploration_fraction>0`, monkeypatch
  `random.random`/`random.choice` for determinism; assert an explore pick can bypass the floor.
- `test_policy_background_operation_ranks_quality_first`: `operation in background_operations`
  → rank rate-first; construct a case where a slow-but-reliable LLM beats a fast-but-worse one.

### C-C. Statistical "no stuck state" (highest value)
- `test_operation_stats_are_isolated`: failures under operation A do not lower `usable_rate` for
  operation B (rolling key is `(llm, operation)`).
- `test_rolling_window_evicts_old_failures`: a once-bad LLM recovers its `usable_rate` after
  `rolling_window` good calls evict the old failures — no permanent statistical penalty.
- `test_probe_cycles_reset_on_recovery`: probe fails once (cycle=1), then a probe OK →
  `on_probing_success` zeroes the counter; a later failure run starts from 0, not accumulating to retire.
- `test_cold_start_not_gated` + `test_seeded_max_delay_decreases_on_success`: below
  `min_sample_count` the floor does not gate (`usable_rate` None passes); and a seeded `max_delay`
  drops after the first live OK (`on_success`).

---

## Part D — follow-up (optional, separate PR)

Merge the isolated `test_postgres_<port>.py` / `test_mongodb_<port>.py` files into parametrized
`test_state_store.py` / `test_registry.py` / `test_secrets.py` built on the Part A `any_*` fixtures.
This also removes the `pytest.importorskip("asyncpg")` calls in `test_postgres_*.py`, which violate
the project rule (CLAUDE.md: "Never use `pytest.importorskip()`") — the conftest imports `asyncpg` /
`motor` unconditionally at module top, so the shared fixtures need no per-test guard. Keep this out
of the main PR to bound the diff.

---

## Verification

Run after each part:

```bash
invoke pre            # ruff + ruff-format + pyrefly, no errors
python -m pytest      # N passed, zero failures/errors, zero skips
python -m pytest tests/test_broker_integration.py tests/test_optimizer_integration.py -v
```

Docker-backed params (postgres/mongodb) run locally on macOS via testcontainers; only GitHub CI
skips them. A green run must show no skipped tests.

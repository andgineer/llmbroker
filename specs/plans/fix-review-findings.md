# Fix the post-simplification review findings

Source: full-codebase review against `specs/reference/mission.md` (2026-07-05),
plus one naming decision made after the review. Contents: a subsystem rename
(knowledge → store), two confirmed bugs (both reproduced end-to-end), one
missing public-surface feature, one wrong CLI wiring, dead code left from the
pre-rewrite design, stale user docs, and test-coverage gaps that let the top
bug through.

## Rules for the implementer (read first)

- Lint/format/type-check only via `invoke pre` (never call ruff directly). Tests via
  `python -m pytest`. Both must be green after **every numbered step**.
- `pytest.ini` runs `--doctest-modules`: doctests in `src/` execute as tests.
- No in-function imports; no `from __future__ import annotations`; Python 3.11+.
- Never edit `src/llmbroker/__about__.py`; never bump the version.
- Never use `pytest.skip`/`importorskip`/`skipIf`. Testcontainers run locally.
- Never delete a test that reproduces a confirmed bug; port it to the new surface.
- Comments: 1–2 lines, non-obvious WHY only. No plan references in code.

---

## 1. Rename the "knowledge" subsystem to "store"; retire the word "state"

**Decision.** The port formerly called "knowledge" is the broker's own
operations database — the only storage llmbroker itself owns and writes: the
append-only call journal, the admin disabled-verdict map, and any future
operational data (aggregates, per-user settings). "Knowledge" collides with
knowledge-base/RAG vocabulary in the LLM domain and misleads users; the new
name is deliberately open-ended: **store**. The word "state" is retired at the
same time: the "state backends" term and the `state/` default directory both
go away. DB table names (`llmbroker_calls`, `llmbroker_disabled`) and on-disk
file formats do **not** change — only Python identifiers, module paths, the
default directory name, and docs/specs vocabulary. (The single deployed
instance migrates by renaming its `state/` directory by hand; out of scope.)

This step lands **first** so every later step is written and implemented in
the new vocabulary.

**a. Module renames** (use `git mv`):

| From | To |
|---|---|
| `src/llmbroker/protocols/knowledge.py` | `src/llmbroker/protocols/store.py` |
| `src/llmbroker/standalone/knowledge.py` | `src/llmbroker/standalone/store.py` |
| `src/llmbroker/sqlite/knowledge.py` | `src/llmbroker/sqlite/store.py` |
| `src/llmbroker/postgres/knowledge.py` | `src/llmbroker/postgres/store.py` |
| `src/llmbroker/mongodb/knowledge.py` | `src/llmbroker/mongodb/store.py` |
| `tests/test_knowledge.py` | `tests/test_store.py` |
| `tests/test_knowledge_backends.py` | `tests/test_store_backends.py` |

**b. Class and identifier renames** (global across `src/` and `tests/`;
update every importer — no re-export shims):

| From | To |
|---|---|
| `KnowledgeProtocol` | `StoreProtocol` |
| `QueryableKnowledgeProtocol` | `QueryableStoreProtocol` |
| `FileKnowledge` | `FileStore` |
| `InMemoryKnowledge` | `InMemoryStore` |
| `class Knowledge` in `sqlite/store.py`, `postgres/store.py`, `mongodb/store.py` | `class Store` |
| `backends/ports.py`: `StoreKnowledge` | `DriverStore` |
| `backends/ports.py`: `StoreRegistry` | `DriverRegistry` |
| `backends/ports.py`: `StoreSecrets` | `DriverSecrets` |

The `Store*` → `Driver*` prefix change in `backends/ports.py` is required:
the old prefix meant "backed by the generic `Driver`", and keeping it would
produce `StoreStore`. `Driver*` matches the layer's actual contract name.
`DisabledMapProtocol` is unchanged.

**c. Keyword argument and internals**: `knowledge=` → `store=` on
`AsyncBroker.__init__` (`broker/broker.py`) and the sync `Broker.__init__`
(`sync.py`). Rename the internal names accordingly: `_default_knowledge` →
`_default_store`, `_base_knowledge` → `_base_store`, `_knowledge` → `_store`,
`effective_knowledge` → `effective_store`, `source_knowledge` →
`source_store`; `broker/source.py` docstrings ("registry/secrets/knowledge
triple" → "registry/secrets/store triple").

**d. Default directory** `state/` → `store/`: in `broker/broker.py`
`_default_store` returns `FileStore(registry.path.parent / "store")` and
`FileStore(Path("store"))`; update the `state/`-mentioning docstrings in
`broker/broker.py`, `broker/source.py`, and `standalone/store.py`.

**e. Public API surface**: `src/llmbroker/__init__.py` re-exports and
`__all__` now carry `FileStore` / `InMemoryStore`. The `_require_queryable`
error message in `broker/broker.py` names
`llmbroker.sqlite.store.Store`. Sweep all remaining docstrings, comments, and
log/error messages in `src/` and `tests/` that use "knowledge" for the
subsystem — including subpackage `__init__.py` docstrings and the conftest
fixture names: `any_knowledge` → `any_store`, `queryable_knowledge` →
`queryable_store`, import aliases like `Knowledge as MongoKnowledge` →
`Store as MongoStore`.

**f. Specs** (`specs/reference/`):

- `architecture.md`: the port table row becomes
  `**store** | StoreProtocol | FileStore(path) ('store/' dir) | …`; update the
  Contracts bullet, the one-liner defaults paragraph
  (`llmbroker.FileStore`, variants `InMemoryStore`), the explicit-arguments
  line (`registry=`/`secrets=`/`store=`), the Batteries table row
  (`FileStore(path)`, `InMemoryStore()`, `llmbroker.sqlite.Store` …), and
  every other "knowledge" mention (grep the file). Add one defining sentence
  where the port table introduces the row: *"store is the only storage
  llmbroker owns and writes: the append-only call journal, the admin
  disabled-verdict map, and any future operational data (aggregates,
  per-user settings)."*
- `mission.md`: items 6–7 ("knowledge journal" → "store journal";
  "registry + knowledge + secrets" → "registry + store + secrets").
- `decisions.md`: replace every subsystem use of "knowledge" with "store"
  and every `state/` path with `store/` (journal at `store/calls/…`,
  verdicts at `store/disabled.yml`, "default is `store/` in the CWD", the
  standalone row in the sizing table). The heading-level decision "Model
  knowledge is an internal llmbroker subsystem, not logging" becomes "The
  store is an internal llmbroker subsystem, not logging".
- `optimizer.md`: the `state/` persistence mention → `store/`.
- **Do not touch** `specs/reference/freetier-providers.md` — its "knowledge"
  is plain English ("curated knowledge llmbroker ships"), not the subsystem.

**g. Retire "state backends"**: `CLAUDE.md` architecture note "State
backends are optional submodules: SQLite, Redis, Postgres, MongoDB" →
"Backends (SQLite, Redis, Postgres, MongoDB) are optional submodules — all
optional extras". `docs/src/en/installation.md` "To use a state backend,
install the matching extra" → "To use a database backend, install the
matching extra". (The rest of `docs/src` is handled in step 10 — do not
rewrite `usage.md` here.)

**Done when** (besides green `invoke pre` + pytest):

- `grep -rn "knowledge" src/ tests/` → nothing (case-insensitive too:
  `grep -rni "knowledge" src/ tests/`).
- `grep -n "knowledge" specs/reference/architecture.md specs/reference/mission.md specs/reference/decisions.md specs/reference/optimizer.md` → nothing.
- `grep -rn "state backend" CLAUDE.md docs/ specs/` → nothing.
- `grep -rn '"state"' src/` → nothing.

## 2. The default file store must feed the journal rebuild (critical)

**Bug** (described post-rename). `QueryableStoreProtocol`
(`protocols/store.py`) requires both `calls()` and `metrics()`; `FileStore`
implements only `calls()`, so every
`isinstance(..., QueryableStoreProtocol)` gate fails for the **default**
store:

- `_LearningHook.maybe_rebuild` (`broker/learning.py:113`) never reads the
  journal → learning does **not** survive a restart (reproduced: 10 ratings →
  demoted; new broker over the same TOML → not demoted), peer cooldowns never
  propagate, `metrics_cache` stays empty — mission items 3, 5, 6 broken in the
  one-liner setup.
- `AsyncBroker.calls()` raises `TypeError` (`broker/broker.py:291`).
- `PoolView._metrics_map` / `AsyncLLM.metrics` fall through to empty.

**Fix — narrow the queryable contract to `calls()` and make metrics a pure
function over rows** (the broker already never uses backend `metrics()`; it is
documented as served from the cached tail):

- `protocols/store.py`: `QueryableStoreProtocol` keeps only
  `calls(*, limit, scope=None)`. Delete the `metrics()` method (and with it the
  last `user_id` parameter in the codebase).
- `backends/ports.py`: delete `DriverStore.metrics()` and
  `_METRICS_SCAN_LIMIT` (it was an unbounded 1M-row scan; also its
  `match={"scope": None}` default silently filtered to `scope IS NULL`,
  excluding all scoped rows).
- `broker/learning.py`: extract the metrics fold out of
  `_apply_scores_and_metrics` into a module-level function, placed right
  above the `_LearningHook` class:

  ```python
  def metrics_from_calls(rows: list[Call]) -> dict[str, LLMMetrics]:
      """rows newest-first: the first call row per model is its most recent."""
      metrics: dict[str, LLMMetrics] = {}
      for row in rows:
          if row.kind != "call":
              continue
          existing = metrics.get(row.llm_name)
          if existing is None:
              metrics[row.llm_name] = LLMMetrics(
                  call_count=1, last_status=row.status, last_at=row.ts,
              )
          else:
              metrics[row.llm_name] = LLMMetrics(
                  call_count=existing.call_count + 1,
                  last_status=existing.last_status,
                  last_at=existing.last_at,
              )
      return metrics
  ```

  `_apply_scores_and_metrics` keeps only the score-bucketing loop and ends
  with `self.metrics_cache = metrics_from_calls(rows)` (one extra pass over
  ≤300 rows is fine; do not try to keep the single-pass merge).
- `broker/pool_view.py` `_metrics_map` and `broker/result.py`
  `AsyncLLM.metrics`: the non-hook queryable branch becomes
  `metrics_from_calls(await store.calls(limit=_DEFAULT_QUALITY_REBUILD_LIMIT))`
  — import both names from `llmbroker.broker.learning`. Behavior for
  `optimize=False` + a queryable backend is preserved (metrics semantics were
  already "over the last N records").
- **Pitfall — isinstance branch order.** After the narrowing, `_LearningHook`
  itself satisfies `QueryableStoreProtocol` (it has `calls()`; before, the
  missing `metrics()` excluded it). `PoolView._metrics_map` and
  `AsyncLLM.metrics` must keep checking `isinstance(..., _LearningHook)`
  **before** the protocol check, so the hook's cache keeps winning over a
  redundant journal read. Do not reorder those branches.

After this, `isinstance(FileStore(...), QueryableStoreProtocol)` is
True with zero additions to `FileStore`; `InMemoryStore` stays
non-queryable (the explicit opt-out — it has no `calls()`) and keeps the
`calls()` TypeError path.

**Tests** (these are the gaps that let the bug through). Private attributes
(`broker._store`, `broker._optimizer`, `broker._learning_hook`,
`broker._pool`) are fine in tests — existing tests already use them; there is
no public way to force a rebuild.

New file `tests/test_file_learning.py`. Shared helper: write a one-model TOML
and set its key env var:

```python
def _toml(tmp_path, name="m1", ref="FL_KEY"):
    p = tmp_path / "llms.toml"
    p.write_text(
        f'[[llms]]\nname = "{name}"\nbase_url = "https://x/v1"\n'
        f'model = "m"\napi_key_ref = "{ref}"\n'
    )
    return p
```

- `test_learning_survives_restart_on_default_file_store` (the exact review
  repro — a confirmed-bug test, never delete):

  ```python
  async def test_learning_survives_restart_on_default_file_store(tmp_path, monkeypatch):
      monkeypatch.setenv("FL_KEY", "sk-test")
      toml = _toml(tmp_path)
      b1 = AsyncBroker(str(toml))
      await b1.ensure_pool()
      for _ in range(10):  # quality_min_count=10; 10 zeros → wilson upper ≈0.28 < floor 0.3
          await b1._store.record_quality("m1", "summarize", 0.0)
      assert b1._optimizer.is_demoted("m1", "summarize")

      b2 = AsyncBroker(str(toml))  # fresh process over the same TOML
      await b2.ensure_pool()       # warm start must reload windows from store/calls/
      assert b2._optimizer.is_demoted("m1", "summarize")
  ```

- `test_calls_works_on_default_file_store`: same setup; broker 1 records one
  `record_quality`, a second broker over the same TOML then returns it via
  `await b2.calls(limit=10)` — non-empty, row has `kind == "quality"`.
- `test_two_brokers_converge_over_one_journal`, parametrized
  `store_kind in ("file", "sqlite")`:
  - file: both brokers constructed over the same TOML path — they share the
    `store/` sibling; `ensure_pool()` both.
  - sqlite: both over the same `str(tmp_path / "b.db")` source string;
    the registry starts empty, so first `await a.sync(str(toml))` (env
    bootstrap copies the key into the DB secrets), then `ensure_pool()` both —
    otherwise `ensure_pool` fail-fasts on the empty registry.

  Both brokers must resolve the **same key value** (one env var, which for
  sqlite `sync` also bootstraps into the DB) — peer 429 cooldowns are gated
  by `key_hash`, so differing keys would make the test silently pass the
  wrong way. Skeleton for the file variant:

  ```python
  until = datetime.now(UTC) + timedelta(seconds=60)
  await a._store.record(Call(
      id=str(uuid.uuid4()), llm_name="m1", operation=None, trace_id=None,
      status=CallStatus.RATE_LIMITED, ts=datetime.now(UTC), http_status=429,
      cooldown_until=until, key_hash=key_hash("sk-test"),
  ))
  for _ in range(10):
      await a._store.record_quality("m1", "summarize", 0.0)

  await b._learning_hook.maybe_rebuild(force=True)
  assert b._optimizer.is_demoted("m1", "summarize")
  assert b._pool._slots["m1"].cooldown_until == until
  ```

- `tests/conftest.py`: add a `"file"` param to `any_store`
  (`FileStore(tmp_path_factory.mktemp("any_store_file"))`) and to
  `queryable_store`; fix the `queryable_store` docstring (it cites
  the now-deleted `metrics()`).
- `tests/test_optimizer_integration.py`: fix the module docstring — it calls
  `InMemoryStore` "the project default", which is wrong (the default is
  `FileStore`).
- `tests/test_store_backends.py`:
  - `test_metrics_counts_calls_per_llm`: replace
    `m = await queryable_store.metrics()` with
    `m = metrics_from_calls(await queryable_store.calls(limit=100))`;
    assertions unchanged.
  - `test_metrics_since_filters_past_calls` and
    `test_metrics_user_id_scoping`: delete — `since`/`user_id` die with the
    method; scope filtering of `calls()` is already covered by
    `test_calls_scope_filter`.

**Done when:** the restart-persistence test passes; `grep -rn "def metrics"
src/llmbroker/backends src/llmbroker/protocols` returns nothing;
`grep -rn "user_id" src/` returns nothing.

## 3. `_wake_timeout` misses wake sources on in-flight slots (critical)

**Bug.** `pool.py:160` skips any slot with `slot.in_flight` truthy when
computing the next wake-up, but the availability predicate is
capacity-aware (`cap is None or in_flight < cap`). With the default
`parallel=None`, a slot with one in-flight sibling and an expiring cooldown
becomes available by pure time passage, yet the waiter sleeps until the next
`notify_all()` (the sibling's release) — reproduced: cooldown 0.2 s, waiter
still blocked at 2 s.

**Fix.** In `_wake_timeout`, skip a slot only when it is at capacity:

```python
cap = slot.config.parallel
if slot.key is None or slot.disabled or (cap is not None and slot.in_flight >= cap):
    continue
```

**Tests** (`tests/test_pool.py`). Note `cool_down` decrements `in_flight`, so
the "sibling still running during the cooldown" state is set by writing the
slot field directly — that is the intended emulation, not a hack:

- bug-repro test (never delete):

  ```python
  async def test_waiter_wakes_on_cooldown_expiry_with_inflight_sibling():
      pool = LLMPool()
      cfg = LLMConfig(name="a", base_url="u", model="m", api_key_ref="K")  # parallel=None
      await pool.add(cfg, "key")
      await pool.acquire(None)
      await pool.cool_down(cfg, 0.1)        # decrements in_flight back to 0
      pool._slots["a"].in_flight = 1        # emulate a sibling call still running
      picked = await asyncio.wait_for(pool.acquire(None), timeout=1.0)  # was: stalls
      assert picked.name == "a"
  ```

- converse guard: same setup but `parallel=1` — a finite `acquire(wait=0.3)`
  still raises `TimeoutError` (cooldown expiry alone must not admit a slot at
  capacity).

## 4. `disabled` on the `get(name)` handle

`specs/reference/optimizer.md` (and the design decision in
`specs/reference/decisions.md`) state the handle exposes the admin verdict;
only `snapshot()` does today.

- `broker/result.py` `AsyncLLM`: add `@property def disabled(self) -> bool:`
  returning `self._pool.is_disabled(self._name)` (the pool flag *is* the
  cached disabled map — `_resync_disabled` keeps it fresh).
- `sync.py`: `LLM` gains a `disabled` property; add `Broker.disabled_of(name)`
  delegating like `config_of`.
- Tests (`tests/test_broker.py`, `tests/test_broker_disable.py` after step 6's
  rename): `disable_llm`/`enable_llm` round-trips through
  `(await broker.get(name)).disabled`, and it matches the `snapshot()` field.

## 5. CLI `sync` must use source dispatch (wrong disabled-map target)

**Bug** (described post-rename). `cli.py:_cmd_sync` builds
`AsyncBroker(registry=SqliteRegistry(args.db))`; the default-store rule
then picks `FileStore("./store")` under CWD, so `sync` seeds a stray
`./store/disabled.yml` and the target DB's `llmbroker_disabled` table is never
seeded (contradicting `architecture.md`).

**Fix.** `_cmd_sync` builds `AsyncBroker(args.db)` — source dispatch wires
`DriverStore` on the same DB (and the command stops being sqlite-only:
`postgresql://` / `mongodb://` work for free). Drop the module-level
`try: from llmbroker.sqlite.registry import ...` fallback; catch `ImportError`
from `resolve_source` and print its actionable "pip install llmbroker[...]"
message. Rename the positional arg help to say "sqlite path or
postgresql:// / mongodb:// URL". Update the subcommand description and
`architecture.md`'s CLI bullet accordingly.

**Tests** (`tests/test_cli.py`): after `sync preset.toml <tmp>/x.db`, the
sqlite `llmbroker_disabled` table contains every preset name with
`disabled=0`; no `store/` directory appears under the test CWD
(`monkeypatch.chdir(tmp_path)`).

## 6. Dead code and leftovers

All confirmed zero-importer (outside their own tests/doctests):

- `models.py`: delete `QualitySummary` (lines 90–169), `reconcile()`
  (71–87), `LLMState.to_dict`/`from_dict`/`extra`/`_RESERVED_STATE_KEYS`
  (state-store serialization leftovers; `LLMState` keeps `phase`,
  `cooldown_until`, `fail_count` — the pool and public handles still use it).
- `tests/test_state.py`: delete — it exists only to exercise the code above;
  phase/fail-count behavior is already covered via `pool.state()` in
  `tests/test_pool.py`.
- `exceptions.py`: delete `SecretsReadOnlyError` (never raised) and its
  re-export in `llmbroker/__init__.py`.
- `backends/spec.py`: replace the "Bump rationale …" comment block (lines
  82–86) — it references plan numbers, which is banned; version-bump history
  belongs to git. Keep at most one line stating what the marker gates.
- `protocols/registry.py` `KeyInfoProtocol` docstring: "(effort, value,
  help)" → "(help text plus a free-form `extra` passthrough)" — the taxonomy
  vocabulary is gone.
- `tests/test_broker_bench.py`: rename to `tests/test_broker_disable.py`
  (the "bench" vocabulary is retired); drop the `user_id=None` parameters
  from its `_EmptySource`/`_OneConfigSource` helpers.

## 7. `LLMSnapshot.demoted_operations` typing

The tuple can contain `None` (the unlabeled-operation bucket) but is typed
`tuple[str, ...]`. Change to `tuple[str | None, ...]` in `models.py` and in
the `PoolView.snapshot()` construction; one docstring line that `None` is the
bucket for calls made without `operation=`.

## 8. Demotion flip logs name the bound

`optimizer.md` says flip lines name "the model, operation, and bound";
`Optimizer._log_flip` omits the bound. Include it:
`"%s: quality-demoted for operation=%r (wilson upper %.3f < floor %.2f)"` on
demote, and the current bound on clear (via `self.wilson_bound(...)`; it can
be `None` right after `load_scores` wipes a bucket — fall back to omitting the
parenthesis then). Update the `caplog` assertions in `tests/test_optimizer.py`.

## 9. Missing failover test (mission item 1)

`tests/test_router.py` covers failover-to-next-LLM for 500, network errors,
and 401, but the flagship 429 path only asserts the `wait=0` raise. Add
`test_http_429_fails_over_to_next_llm` mirroring
`test_http_500_fails_over_to_next_llm`: first LLM answers 429 (with a
`Retry-After`), second answers OK, default `wait` — the caller gets the OK
result in the same request and the first LLM is COOLING.

## 10. Rewrite `docs/src/en/usage.md` and `docs/src/ru/usage.md`

Both files still document the pre-rewrite API and fail on every example:
`stack=` + `Stack` classes, `seed=`/`seed_policy=` + the `SeedPolicy` table,
`purge_calls`, `user_id=`, a Redis state store, "telemetry", `EffortLevel` in
the `key_info()` example, a "Learned profile" section, and "Manual bench".
Nothing else under `docs/src` is stale (`cli.md`, `index.md`,
`installation.md` checked clean; step 1 already touched `installation.md`'s
"state backend" wording).

Rewrite against the current API — source of truth is
`specs/reference/architecture.md` + `optimizer.md`, not memory. The rewritten
pages must not use the words "knowledge" or "state store"; the subsystem is
"the store", the directory is `store/`. Target section-by-section structure
(replaces the current heading tree; keep the same `# Usage` top level):

1. **Configuration** — keep as-is (TOML + `python -m llmbroker env`),
   except the `key_info()` example at lines 61–62: the output shows
   `EffortLevel`/`ValueLevel`, which no longer exist — show the real shape
   (`KeyInfo(api_key_ref=..., help=..., extra={"effort": "signup", ...})`,
   the TOML section as a free-form passthrough).
2. **Calling the broker** (ask/chat, the sync wrapper) — survives; verify the
   snippets against the current signatures.
3. **Controlling requests** — `wait` and `operation` subsections survive;
   "Quality feedback" stays but must state the current semantics: the score
   lands in the journal as a self-contained record and feeds the
   per-(model, operation) window.
4. **Learning & selection** — new section replacing "Learned profile"
   (lines 159–170): curated preset order, demoted-for-this-operation sorts
   last, demotion is soft (a demoted-only pool still serves), recovery =
   new ratings displacing the window, no reset exists.
5. **Administration** — replaces "Manual bench" (lines 171–183) and the
   pool-management half of the SQLite section: `disable_llm`/`enable_llm`,
   the hand-editable `store/disabled.yml`, `get(name).disabled` (step 4),
   `snapshot()` raw facts (`disabled`, `has_key`, `cooldown_until`,
   `demoted_operations`, metrics), `calls(limit=...)`.
6. **Production** — "Closing the broker" survives; the rest is rewritten:
   the source parameter (`Broker("config.toml")`, `"llm.db"`,
   `"postgresql://…"`, `"mongodb://…"`) with explicit
   `registry=`/`secrets=`/`store=` overrides replaces "Mixing backends"
   (the `Stack`/Redis text at lines 258–326 dies); explicit `sync(preset)`
   (mirror semantics, identity-change refusal, empty-registry fail-fast) and
   `python -m llmbroker sync` replace the `SeedPolicy` table (296–304);
   journal retention (constructor parameter, default 90 days) replaces
   `purge_calls` (line 293); a short note on `parallel = 1` for finicky
   providers.
7. **Multi-user** — rewrite the section at lines 327+ around `scope=`:
   own key `{scope}/{ref}` falling back to the shared ref, learning and the
   registry stay global, quota follows the key; delete `user_id=` and
   `state_store=` text.
8. **Tools & agents**, **AWS / Vault secrets** — keep; verify each snippet
   against current signatures rather than assuming.

The RU page mirrors the EN page section-for-section (in Russian, per the
existing convention — in-repo docs under `docs/src/ru/` are the one sanctioned
non-English surface); write EN first, then translate. Verify with
`invoke docs-en` building cleanly and every fenced python block exercised by
a scratch script (`exec` against a temp TOML with a fake env key; no network
calls in snippets — anything that would call a provider stays illustrative
and is excluded from the exec check by marking the fence as `python title=...`
consistently with how the current page marks non-runnable examples, or by
keeping such lines commented).

## 11. Spec touch-ups

- `architecture.md`: the backend-import bullet and the Batteries table name
  `llmbroker.sqlite.Registry`, `llmbroker.postgres.Secrets`, … — **not
  importable** (subpackage `__init__.py` is docstring-only by convention).
  Correct to the named-module paths: `llmbroker.sqlite.registry.Registry`,
  `llmbroker.sqlite.store.Store`, `llmbroker.sqlite.secrets.Secrets`,
  and likewise for postgres/mongodb/aws/vault.
- `architecture.md` CLI bullet: `sync` accepts any DB source (step 5).
- `optimizer.md`: no changes needed beyond what steps 1, 4, and 8 make true.

---

## Step order

1. **1** knowledge → store rename (first, so every later step lands in the
   new vocabulary and nothing gets edited twice)
2. **2** file-store rebuild (the critical bug — lands alone)
3. **3** `_wake_timeout` capacity fix
4. **4** `disabled` handle property
5. **5** CLI sync dispatch
6. **6 + 7 + 8** dead code, snapshot typing, flip-log bound (one cleanup pass)
7. **9** 429-failover test
8. **10** docs rewrite (EN, then RU mirror)
9. **11** spec touch-ups

**Final gate:** `invoke pre` + full `python -m pytest` green, zero skips;
`invoke docs-en` builds;
`grep -rni "knowledge" src/ tests/ docs/src/` returns nothing;
`grep -rn "state backend" CLAUDE.md docs/ specs/` returns nothing;
`grep -rn "user_id\|SecretsReadOnlyError\|QualitySummary" src/ docs/src/` returns nothing.

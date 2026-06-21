# Plan: replace AsyncBroker dict interface with explicit accessors

Date: 2026-06-21

## Problem

`AsyncBroker(Mapping[str, AsyncLLM])` exposes synchronous `__getitem__` /
`__iter__` / `__len__` that read `self._configs` directly and never call
`ensure_pool()`. On a fresh broker they silently return empty/stale data until
some `await` path happens to warm the pool — a trap, because a sync protocol
cannot `await ensure_pool()`. The sync `Broker` wrapper hides this by blocking,
so the bug is `AsyncBroker`-specific.

## Decisions (from brainstorm)

- Drop the `Mapping` interface entirely (no `__getitem__`/`__iter__`/`__len__`).
- Identity stays **by name**, not by index. No `index()`, no positional access.
- Do **not** turn the broker into an async generator: all inspection IO
  (`telemetry.metrics()`, `shared_state.read()`) must be batched up front, so a
  generator would be eager work disguised as iteration. Keep `snapshot()`.
- Replace the dict surface with explicit async accessors, each calling
  `ensure_pool()` first:
  - `get(name) -> AsyncLLM` — lazy handle; `config` is free (local), `state()` /
    `metrics()` do IO only on demand. Raises `KeyError` if absent.
  - `count() -> int` — cheap: `ensure_pool()` + `len(self._configs)`, no IO.
- Split the upsert `add` into two explicit operations:
  - `add(cfg)` — create; raises `ValueError` if the name already exists.
  - `update(cfg)` — modify existing; raises `KeyError` if the name is absent.
- `snapshot()` keeps its signature but merges cluster state: one
  `shared_state.read()` (whole-dict, batched) with `InMemoryState` as fallback.
  `AsyncLLM.state()` uses the same merge for consistency.
- `remove(name)` unchanged (by name).
- Exceptions: plain `KeyError` (missing) / `ValueError` (duplicate). No domain
  exception classes for now.

## Changes

### `src/llmbroker/broker.py`

1. **`AsyncLLM`** — give it access to shared state for the merge:
   - Add `shared_state: SharedStateProtocol | None` param to `__init__`.
   - `state()` becomes:
     ```python
     async def state(self) -> LLMState:
         if self._shared_state is not None:
             shared = await self._shared_state.read()
             if self._name in shared:
                 return shared[self._name]
         return self._state.get_state(self._name)
     ```

2. **`AsyncBroker` class declaration** (line 136):
   - Drop the base class: `class AsyncBroker:` (was `Mapping[str, AsyncLLM]`).
   - Remove the `Mapping interface` block (lines 235–244): `__getitem__`,
     `__iter__`, `__len__`.
   - Remove now-unused `Iterator` import; keep `Mapping` (still used as the
     `snapshot()` return annotation).

3. **New accessors** (place near where the Mapping block was):
   ```python
   async def get(self, name: str) -> AsyncLLM:
       await self.ensure_pool()
       if name not in self._configs:
           raise KeyError(name)
       return AsyncLLM(
           name, self._configs[name], self._state, self._telemetry,
           shared_state=self._shared_state,
       )

   async def count(self) -> int:
       await self.ensure_pool()
       return len(self._configs)
   ```

4. **`snapshot()`** (lines 438–455) — merge cluster state:
   ```python
   shared: dict[str, LLMState] = {}
   if self._shared_state is not None:
       shared = await self._shared_state.read()
   ...
   state = shared.get(name) or self._state.get_state(name)
   result[name] = LLMSnapshot(config=cfg, state=state, metrics=metrics)
   ```

5. **Split `add`** (lines 470–474) into `add` + `update`:
   ```python
   async def add(self, cfg: LLMConfig) -> None:
       await self.ensure_pool()
       registry = self._require_mutable_registry()
       if cfg.name in self._configs:
           raise ValueError(f"LLM {cfg.name!r} already exists; use update()")
       await registry.add(cfg)
       await self._add_to_pool(cfg)

   async def update(self, cfg: LLMConfig) -> None:
       await self.ensure_pool()
       registry = self._require_mutable_registry()
       if cfg.name not in self._configs:
           raise KeyError(cfg.name)
       await registry.update(cfg)
       await self._add_to_pool(cfg)  # is_new is False → config replaced, no enqueue
   ```

### `src/llmbroker/sqlite.py` — `Registry`

- `add`: remove the `ON CONFLICT(name) DO UPDATE` clause → plain `INSERT`. Wrap
  the execute so `sqlite3.IntegrityError` (UNIQUE) surfaces as
  `ValueError(f"LLM {cfg.name!r} already exists")`. This is a backstop; the
  broker checks `_configs` first.
- `update`: stop aliasing `add`. Real statement:
  `UPDATE llmbroker_registry SET base_url=?, model=?, api_key_ref=? WHERE name=?`.
  If `cursor.rowcount == 0` raise `KeyError(cfg.name)`.

### `src/llmbroker/sync.py` — `Broker`

- Drop the `Mapping[str, LLM]` base and the `__getitem__`/`__iter__`/`__len__`
  block (lines 116–128).
- Rework the `LLM`-companion accessors that used `self._async[name]`:
  - `config_of`: `return self._run(self._async.get(name)).config`
  - `state_of`: `llm = self._run(self._async.get(name)); return self._run(llm.state())`
  - `metrics_of`: `llm = self._run(self._async.get(name)); return self._run(llm.metrics(since=since))`
- Add mirrors:
  ```python
  def get(self, name: str) -> LLM:
      self._run(self._async.get(name))  # raises KeyError if absent
      return LLM(self, name)

  def count(self) -> int:
      return self._run(self._async.count())

  def update(self, cfg: LLMConfig) -> None:
      self._run(self._async.update(cfg))
  ```
- Keep `snapshot()`, `add()`, `remove()`. `_ensure_pool()` is still used by
  `__enter__`; keep it.

### `src/llmbroker/__init__.py`

- No export changes. `AsyncLLM` / `LLM` stay public; `get`/`count`/`update` are
  methods, not top-level symbols.

## Tests (same session)

- `tests/test_broker.py`:
  - `broker["p1"]` → `await broker.get("p1")`; `broker["nope"]` KeyError →
    `with pytest.raises(KeyError): await broker.get("nope")`.
  - `len(broker)` → `await broker.count()`.
  - Add cases: `add` of an existing name raises `ValueError`; `update` of an
    absent name raises `KeyError`; `update` of an existing name changes config
    without adding a queue slot.
  - `snapshot()` reflects `shared_state` when a shared backend is wired (add a
    fake `SharedStateProtocol` returning a cooling state and assert snapshot/
    `get(name).state()` pick it up over `InMemoryState`).
- `tests/test_sync.py`: `broker["p1"]` → `broker.get("p1")`; `len(broker)` →
  `broker.count()`; add `update()` path.
- `tests/test_secrets.py`: `broker["p1"].config` → `(await broker.get("p1")).config`.

## Docs

- `docs/src/en/usage.md` and `docs/src/ru/usage.md`: the `snapshot().items()`
  example stays valid. Add an `update(...)` example next to `add(...)`. Replace
  any `broker[name]` / `len(broker)` examples with `get(name)` / `count()`.

## Release

- Breaking API change (pre-1.0). Bump with `invoke ver-feature`.
- This release is the trigger for the companion dinary plan
  (`specs/plans/<date>-llmbroker-broker-interface.md` in dinary).

## Done gate

1. `invoke pre` — clean (ruff + pyrefly).
2. `python -m pytest` — all pass (doctests included via `--doctest-modules`).

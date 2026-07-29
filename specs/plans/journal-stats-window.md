# Journal statistics over a time window

This plan is the suggested route; if the code has drifted from what it assumes, the code wins.

Motivating consumer: dinary's LLM admin screen wants "how many of this provider's calls failed
over the last 7 days" and today has no way to get it that does not involve re-implementing
llmbroker's journal model inside the host.

## Context

The journal (`llmbroker_calls`) has exactly one public read path:
`QueryableStoreProtocol.calls(*, limit, scope)` — a newest-first tail of raw `Call` rows,
implemented once in `backends/ports.py` over the driver primitive
`recent(table, limit, match)`, and separately in `standalone/store.py` over day files.

Two consequences:

- **The journal model leaks into every host.** The tail interleaves both record kinds — the
  docstring of `calls()` says so — and a `kind="quality"` row carries `status=None` by
  construction. A host that wants a call-failure ratio must know to drop those rows, or its
  denominator is silently wrong. It must also know that quality records consume the tail budget.
- **Any time window a host derives is approximate.** With only `limit` available, "the last 7
  days" is really "whatever part of the last 7 days fits in the last N rows", and the host cannot
  tell a quiet week from a truncated tail.

What llmbroker already derives from the journal is a different aggregate:
`metrics_from_calls` (`broker/learning.py:33`) → `LLMMetrics(call_count, last_status, last_at)`,
computed over the cached rebuild tail and exposed through `snapshot()`. It answers "how many rows
did the last rebuild see", not "how did this model behave over a period". This plan does not
change it.

`spec.py` already declares `indexes=(("llm_name",), ("called_at",))` on the journal, so a
time-bounded read is an indexed scan on every SQL backend. Every driver already implements a
time-bounded operation — `purge(table, before)` — so the shape is familiar to all of them.

## Design decisions

**Aggregate per request; do not maintain running counters.** A sliding window ("the last 7 days")
cannot be served by accumulated counters: old calls must fall out of the aggregate on their own,
which a monotonic counter cannot express. Doing it with counters means day buckets plus rotation
and subtraction — a second piece of stored state, its own ageing logic, and its own bugs — while
`WHERE called_at >= ? GROUP BY llm_name, status` over an indexed column is one cheap read.

It would also contradict `broker/learning.py`'s own principle: *"No second storage subsystem:
everything llmbroker learns beyond config is re-derived from the append-only journal."* Stored
counters are exactly that second subsystem, and they must eventually disagree with the journal —
on restart, after retention purges, and across nodes writing to one journal. They would also put
an atomic UPDATE on the hot path of every model call.

If read volume ever justifies it, the answer is a TTL cache over this aggregate, not counters:
same saving, with an honest expiry rather than "refreshed only when something failed" — the
failure mode `metrics_cache` already demonstrates.

**Filter in the store, aggregate in Python.** The `since` bound belongs in the store: it is what
makes the window exact and keeps the row count proportional to the window rather than to `limit`.
Aggregating the filtered rows in shared Python code keeps one implementation for all backends; a
`GROUP BY` driver primitive would need a native implementation in each of the four drivers (plus a
Mongo aggregation pipeline) for an aggregate whose input is already bounded by the window. Revisit
only if a real deployment shows the row transfer mattering.

**Policy stays with the host.** llmbroker returns per-status counts; it does not decide what
counts as a failure, how long the window should be, or how a provider with no calls in the window
should read. Baking "failure rate" into the library would repeat the `call_count` mistake: a
number whose meaning is fixed by the library and wrong for the next consumer.

## 1. Driver: a time bound on `recent`

- `backends/driver.py` — `recent(table, limit, match=None, since=None)`. `since` is a
  `datetime | None`; rows at or after it are returned. Document that it applies to the journal's
  `called_at` and is meaningful only for the append-only journal table.
- Implement in all four drivers, mirroring how each already implements `purge(table, before)`:
  - `sqlite/driver.py` (`recent`, ~:190-210) — one more `called_at >= ?` condition in the existing
    `where` assembly, before the `ORDER BY called_at DESC LIMIT ?`. The `called_at` index in
    `spec.py` covers it.
  - `postgres/driver.py`, `mongodb/driver.py`, `backends/inmemory.py` — same bound in each one's
    idiom.
- No change to `TABLES` or `SCHEMA_VERSION`: no new column, no new index.

## 2. Store: expose the window and the kind filter

- `protocols/store.py` — `QueryableStoreProtocol.calls(*, limit, scope=None, since=None,
  kind=None, operation=None)`. All three new parameters default to `None`, so every existing
  caller and every third-party implementation keeps working.
- `backends/ports.py` (`calls`, :175) — pass `since` through and fold `kind` and `operation` into
  the existing `match` dict, which already does equality matching. No new driver primitive for
  either.
- **Why `operation` belongs here:** the journal is shared by everything the broker calls, and
  `llm-judge.md` adds internal traffic under its own operation (`llmbroker.judge`) journaled like
  any other call. Without an operation filter a host's per-model counts silently include broker
  traffic the host never issued, and its failures read as host-visible failures. The filter is one
  more key in a dict that already exists, so the plans compose at no cost.
- `standalone/store.py` (`calls`, :183) — the same two parameters in `_read_tail`. Day files make
  the time bound natural: skip whole files older than `since` before reading rows.
- `broker/broker.py` (`AsyncBroker.calls`, :323) and `sync.py` (:204) — forward both parameters.

## 3. The aggregate

- `models.py` — `LLMStats`: `total`, `by_status: Mapping[CallStatus, int]`, `first_at`, `last_at`,
  `last_status`. Frozen dataclass, like its neighbours. `by_status` holds only statuses actually
  seen, so a host reading "how many were not OK" subtracts rather than assuming the enum's shape.
- New `broker/stats.py` — `stats_from_calls(rows) -> dict[str, LLMStats]`, counting `kind="call"`
  rows only, newest-first input as everywhere else in this codebase.
- `broker/learning.py` — reimplement `metrics_from_calls` as a projection of `stats_from_calls`
  rather than a second traversal with its own kind filter. `LLMMetrics` and its semantics do not
  change; this only removes the duplicate rule about which rows count.

## 4. Public API

- `AsyncBroker.stats(*, since=None, limit=1000, operation=None) -> Mapping[str, LLMStats]` — reads the journal
  through `_require_queryable()` (same `TypeError` contract as `calls`) and returns the aggregate.
  It must not call `ensure_pool()`, and neither must `calls()`: **journal reads never provision.**
  The journal is written by the router and read by the host; its rows do not depend on the
  registry, so a visibility call must keep working on an install whose registry is empty, stale,
  or gone — precisely the state a host UI most needs to render. `mission-conformance-fixes.md`
  states the same rule; the two plans must not diverge on it.
- `SyncBroker.stats` — the mirror wrapper, next to `calls` (:204).
- `snapshot()` and `LLMSnapshot.metrics` are untouched. Their cached-tail semantics stay as
  documented in `specs/reference/decisions.md`; hosts that need a window now have an API that
  does not pretend to be live.
- `limit` stays as a ceiling against an anomalous window (a runaway retry storm), not as the
  window itself. When the returned row count equals `limit` the window may be truncated — document
  that on the method so a host can detect it if it cares.

## 5. Out of scope

- A `failure_rate` / reliability field — host policy, see the design decisions.
- Latency percentiles. `latency_ms` is in the journal and would fit `LLMStats` later; nothing asks
  for it yet, and percentiles want a `GROUP BY` primitive to stay cheap.
- Caching of any kind.
- Changing `snapshot().metrics` or its rebuild triggers.

## 6. Tests

- `tests/test_driver_conformance.py` — the `since` bound for all four drivers in the existing
  journal-ops suite: rows before the bound excluded, rows at the bound included (document the
  boundary as inclusive and test it), `since=None` unchanged, `since` combined with `match`.
- `tests/test_store.py` / `tests/test_store_backends.py` — `calls(kind="call")` drops quality
  records; `calls(since=…)` bounds the window; `calls(operation=…)` keeps only that operation;
  all three together.
- `stats(operation=…)` counts only that operation's rows — the property that keeps a host's
  numbers free of the judge traffic `llm-judge.md` will add.
- `tests/test_file_learning.py` (or the standalone store's own test module) — the day-file store
  honours `since`, including that it skips whole files rather than reading and discarding.
- New `tests/test_stats.py` — `stats_from_calls`: per-status counts, quality rows ignored,
  `first_at`/`last_at`/`last_status` from a newest-first sequence, empty input, a model with only
  quality records.
- `tests/test_learning.py` — `metrics_from_calls` keeps its current results after being rebuilt on
  `stats_from_calls` (regression, not new behaviour).
- `tests/test_broker.py` — `broker.stats()` on an empty registry returns an empty mapping instead
  of raising, and does not provision the pool.
- `pytest.ini` runs `--doctest-modules`: any doctest added to `stats.py` must pass.

## 7. Specs and docs

- `specs/reference/decisions.md` — a decision entry stating that journal aggregates are derived
  per request from the journal, never accumulated, and why (sliding windows, no second storage
  subsystem); and that the library returns per-status counts while failure policy and window
  length belong to the host.
- `specs/reference/architecture.md` — the journal read path gains a time-bounded form; keep it to
  current-state prose.
- `docs/src/en/server.md` and `docs/src/en/disable.md` mention the journal/status APIs — extend
  whichever describes reading the journal with the windowed statistics call.

## Work order and done gate

1. Driver `since` + conformance tests (§1, §6) — everything else rests on it.
2. Store/protocol/broker plumbing for `since` and `kind` (§2).
3. `LLMStats` + `stats_from_calls`, `metrics_from_calls` rebuilt on it (§3).
4. `AsyncBroker.stats` / `SyncBroker.stats` (§4) + tests.
5. Specs and docs (§7).
6. `invoke ver-feature` (additive, no breaking change), then release so the host can consume it.
7. Gate after every batch: `invoke pre` → no ruff/pyrefly errors, `python -m pytest` → `N passed`
   with zero skips.
</content>

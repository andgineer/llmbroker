# Looking a call up by an id it was called with

## Goal

`calls()` gains two filters — `trace_id` and `call_id` — so a caller can retrieve
the rows of one request, or of one attempt, without scanning a tail whose depth it
has to guess. `trace_id` gains an index in every port that can carry one.

This is the lookup half of rating by key, which depends on it. It stands on its
own as observability: "show me every attempt this request
made" is a question the journal holds the answer to and cannot currently be asked.

Today `trace_id` is a dedicated column (`backends/spec.py:57`) with no query path
and no index, which the column policy in `rules/backends.md` does not admit — a
field earns a column only if it appears in a `WHERE`, and this one cannot.

## The decision entry

Lands in `specs/reference/decisions.md` under "## Storage", after
`store-is-not-logging`, in the same batch as the behavior:

```markdown
### host-supplied-fields-earn-the-query-surface

A journal field the library never interprets is still filterable when a host's own
question is asked in its terms.

**Blocks:** keeping `trace_id` stored but unqueryable, on the grounds that
llmbroker itself never routes or learns by it.
**Why:** `scope` is the same shape — host-supplied, uninterpreted — and has been a
filter since the store port existed, so "the library does not read it" was never
the line. The line is whether the field answers a question about the journal, and
one request's attempts are exactly such a question: failover writes a row per
attempt, so without the filter a host rebuilds the set by scanning a tail whose
depth it must guess, and a trace pushed past that depth is indistinguishable from
one that never happened. `store-is-not-logging` does not reach this — it blocks
emitting store events outward into logging, not answering a query over a column
already stored.
```

## Naming trap to get right

On a **call** row, the attempt's id is the `id` column; the `call_id` column is
empty. On a **quality** row, `id` is a fresh uuid and `call_id` carries the
passthrough the host supplied.

So the new `call_id=` filter matches the **`id` column** — it is named for what the
host holds (the id it read off a result handle), not for the column that happens
to share the name. State this in the parameter's docstring line; it is the one
place a reader will guess wrong.

## Work order

### 1. The index — no `SCHEMA_VERSION` bump

`src/llmbroker/backends/spec.py:73` — `TABLES["calls"].indexes` becomes
`(("llm_name",), ("called_at",), ("trace_id",))`. `id` is the table key and needs
no index of its own.

Leave `SCHEMA_VERSION` at 7. It gates the *column* shape, which does not change,
and every driver re-issues the whole index DDL on a fresh process rather than only
on a fresh database:

- sqlite — `_apply_ddl` (`sqlite/driver.py:109`) loops `TABLES` and issues
  `CREATE INDEX IF NOT EXISTS`; `ensure_schema` short-circuits on the
  process-local `_schema_ready` map keyed by db path.
- postgres — `_apply_ddl` (`postgres/driver.py:71`) does the same; `ensure_schema`
  short-circuits on a per-instance flag.
- mongodb — the `for spec in TABLES.values()` loop in `ensure_schema`
  (`mongodb/driver.py:74`) calls `create_index` for every spec index and is **not**
  gated on `current == 0`; `create_index` is idempotent by name.

An existing database therefore gains the index when a process next starts, with no
drop-and-recreate and no `no-schema-migrations` event. A test pins this — it is the
load-bearing claim of this step.

### 2. The port protocol

`src/llmbroker/protocols/store.py:31` — add `trace_id: str | None = None` and
`call_id: str | None = None` to `QueryableStoreProtocol.calls`, keyword-only like
their siblings.

### 3. The DB-backed ports — one edit covers three backends

`src/llmbroker/backends/ports.py:166`, `DriverStore.calls`: add both parameters and
two clauses beside the existing three:

```python
if trace_id is not None:
    match["trace_id"] = trace_id
if call_id is not None:
    match["id"] = call_id
```

`sqlite`, `postgres` and `mongodb` need **no change**: each `recent()` builds its
`WHERE` clause (or query document) generically from the `match` mapping —
`sqlite/driver.py:245`, `postgres/driver.py:181`, `mongodb/driver.py:130`.

### 4. `FileStore`

`src/llmbroker/standalone/store.py` — add both parameters to `calls()` (line 172)
and `_read_tail` (line 130), plus two predicates beside the existing
`scope`/`kind`/`operation` checks.

No index: a day-split JSONL tail has none by construction. The win here is not
speed but meaning — filtering moves inside the store, so `limit` bounds *matching*
rows rather than *scanned* rows.

`InMemoryStore` is not queryable and is untouched.

### 5. Public pass-through

Add both keywords and forward them, changing nothing else:

- `src/llmbroker/broker/llms.py:244` — `AsyncLLMs.calls`
- `src/llmbroker/broker/broker.py:462` — `AsyncBroker.calls`
- `src/llmbroker/sync.py:304` — `Broker.calls`

Docstrings in all five touched call sites stay inside the 3-prose-line cap that
`scripts/check_docstrings.py` enforces.

### 6. Docs

`docs/src/en/server.md` and `docs/src/ru/server.md`, section "Tracing one request"
/ "Трассировка одного запроса": the closing paragraph currently tells the reader
there is no `trace_id` filter and to narrow the tail themselves with a generous
`limit`. Replace it and the example with the filtered call, and say what `limit`
now means — matching rows, not scanned rows. Note that on `FileStore` it remains a
scan, so the guarantee is correctness rather than speed. Mention `call_id=` for one
attempt, with the naming trap stated in one sentence.

Both languages in the same batch; the sections mirror each other line for line.

### 7. Specs

`specs/reference/rules/backends.md`, "The read path": the sentence stating that
both read forms narrow "by an inclusive lower time bound, by record kind, and by
operation" gains the two id dimensions, and records that they are on the tail form
only — the aggregate is per model over a window and has no use for one request.
State that they share the operation filter's "unset means do not filter"
semantics.

### 8. Version

`invoke ver-feature` — the maintainer's, skipped by the implementer.

## Tests

`tests/test_store_backends.py` is backend-parametrized over file / sqlite /
postgres / mongodb via the `queryable_store` fixture, so each test below runs on
all four. First change the `_call` helper (line 17) to take `trace_id=None` as a
parameter instead of hardcoding it.

- `test_calls_trace_id_filter_keeps_only_that_trace` — two traces in, one out.
  Mirrors `test_calls_operation_filter_keeps_only_that_operation`.
- `test_calls_trace_id_spans_a_failover_burst` — three rows under one trace, two
  `RATE_LIMITED` and one `OK`; assert all three return and exactly one is `OK`.
- `test_calls_trace_id_limit_bounds_matching_rows_not_scanned` — record the target
  trace, then several newer rows under another trace; `calls(limit=2,
  trace_id=...)` still returns the target's rows. **Fails before this change** and
  is the behavioural point of the plan.
- `test_calls_call_id_selects_one_attempt` — two call rows; the filter matches the
  `id` column.
- `test_calls_call_id_does_not_match_a_quality_rows_passthrough` — record a call
  row with `id="a"` and a quality row carrying `call_id="a"`; `calls(call_id="a")`
  returns the call row only. Pins the naming trap.
- `test_calls_combines_the_id_filters_with_since_kind_and_operation` — mirrors
  `test_calls_combines_since_kind_and_operation`.
- `test_calls_unset_id_filters_return_every_row` — the "do not filter" semantics.

`tests/test_schema_migration.py`:

- `test_sqlite_ensure_schema_creates_the_trace_index` — assert
  `llmbroker_calls_idx_trace_id` is present via `sqlite_master`, alongside the
  existing table assertions at line 131.
- `test_sqlite_existing_database_gains_a_new_index_without_a_version_bump` —
  create the schema, drop the index by hand, open a **fresh** driver instance on
  the same path, assert the index is back and the version marker is unchanged.
  Pins step 1.
- `test_mongodb_ensure_schema_creates_the_trace_index` — via
  `index_information()`, mirroring the registry-index assertion at line 220.

`tests/test_broker.py`: one pass-through test that both keywords reach the store,
placed with the existing broker-level `calls()` coverage.

No test may use `pytest.skip`/`importorskip`; postgres and mongodb come up under
testcontainers, so Docker must be running.

## Gate

`invoke pre` clean (ruff, ruff-format, pyrefly, docstring cap) and
`python -m pytest` reporting `N passed` with zero failures, errors or skips.

## Handover

### Done as written

Steps 1–7 in full. `SCHEMA_VERSION` stayed at 7; `TABLES["calls"].indexes` gained
`("trace_id",)`; both filters were added to `QueryableStoreProtocol`,
`DriverStore`, `FileStore` and every public pass-through; the `decisions.md` entry
landed verbatim under `## Storage` after `store-is-not-logging`; `rules/backends.md`
"The read path" records the two id dimensions, that they are tail-only, and the
shared "unset means do not filter" semantics; both language docs were rewritten in
the same batch. `sqlite`, `postgres` and `mongodb` needed no change, as the plan
predicted — each `recent()` builds its filter generically from the `match` mapping.

Step 8 (`invoke ver-feature`) skipped — the maintainer's.

### Done differently

**One more pass-through than the plan listed.** The plan named three
(`broker/llms.py`, `broker/broker.py`, `sync.py:304`); `sync.py` has two — the
sync `LLMs.calls` wrapper as well as `Broker.calls`. Both forward the keywords.

**Seven `# noqa: PLR0913` the plan did not anticipate.** `calls()` now takes 7
narrowing parameters against ruff's max of 5, at every layer. The project already
uses this suppression at 15 sites with a short reason, so this follows the local
convention rather than raising the global limit.

**`FileStore._read_tail` took an attribute→value match mapping instead of a sixth
keyword.** Adding the two predicates to the existing `if … is not None` chain
pushed the method past ruff's C901 complexity ceiling (12 > 10) *and* PLR0913. It
now takes the same shape the driver stores already use — a mapping the caller
builds, with `call_id` mapped onto `id` exactly as `DriverStore` maps it onto the
column — which removes both warnings without suppressing either, and makes the two
store implementations mirror each other.

**`DriverStore.calls` and `LLMs.calls` docstrings each lost one clause** to stay
inside the 3-prose-line cap, since the naming trap had to go in. `DriverStore` lost
"unfiltered by scope (learning is global)", which `rules/backends.md` states;
`LLMs` lost "(inclusive `called_at` bound)" shortened to "(inclusive bound)".

### Decisions taken during implementation

- The plan's sqlite fresh-process test reuses the existing `_ensure_schema` helper
  in `test_schema_migration.py`, which pops the per-path `_schema_ready` memo —
  without it a second driver on the same path short-circuits in-process and the
  test would pass for the wrong reason.
- `test_calls_combines_the_id_filters_with_since_kind_and_operation` asserts three
  times rather than once: `call_id` pins a single row, so combining it with the
  other four filters would make them non-load-bearing. The first assertion proves
  `trace_id` alongside since/kind/operation; the other two prove `call_id` narrows
  further and excludes.
- Docs gained one line beyond the plan: the "Call journal" intro in both languages
  now names the id filters and links to the tracing section, since that intro
  enumerates what a read can narrow by.

### Gate

`invoke pre` — clean (ruff, ruff-format, docstring cap, pyrefly 0 errors).
`python -m pytest` — **1326 passed**, zero failures, errors or skips. Docker was
running, so the postgres and mongodb testcontainer parametrizations all ran.

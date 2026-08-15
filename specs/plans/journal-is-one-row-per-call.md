# The journal a host reads is one row per call

## Goal

`calls()` returns one row per call attempt, carrying the score it was rated with
or `None`. The two record kinds stop existing above the storage layer: no `kind`
filter, no interleaved stream, no public type that is a call *or* a rating.

The fold happens in the driver, in one round-trip per backend, so nothing above it
matches rows by id. `Driver.recent` is **replaced** by `Driver.journal_view` — the
driver's operation count does not change.

Every rating names the call it rates. The `(llm_name, operation, score)` form is
removed: with reads folded onto calls it is a write nothing reads.

## What this supersedes

A separate queued plan for the write side (`record_quality` taking a key the host
already holds) is folded into this one and its file is gone: with reads folded
onto calls, the two cannot ship apart. What it planned survives here in reduced
form — one key form instead of three, and no write-time resolution of
`(llm_name, operation)`, since the view reads both off the call.

`journal-lookup-keys.md` (already implemented) is forward-compatible: its
`trace_id`/`call_id` filters keep their meaning. Its docs paragraph about the
naming trap is deleted by step 8 here, since the trap it warns about is gone.

`routed-call-identity.md` is unaffected and stays queued after this one.

## The decision entries

Both land in `specs/reference/decisions.md` in the same batch as the behavior.

`self-contained-quality-records` (line 63) is **replaced** by this entry, in
place, under `## Learning and quality`:

```markdown
### a-rating-names-the-call-it-rates

A rating is an appended row carrying a call's id and a score; the model and the
operation it counts toward are read off that call.

**Blocks:** storing the model and operation on the rating itself, so that a rating
naming no call — or naming one the journal no longer holds — still counts.
**Why:** every quality signal a host has is about a specific answer, so a rating
that names no call was never a real shape. Storing the pair on the rating let it
disagree with the call about which model and which operation were rated, with
nothing able to catch it; reading them off the call makes the disagreement
impossible and lets one read answer the whole question, as one row per call. Two
things stop counting and both were already worth nothing: a rating whose call
retention has purged lands in a window rebuilt from a tail far shorter than the
retention, so it reached a window that had forgotten everything around it; and a
second rating of the same call is the host changing its mind, which should not
vote twice. The journal stays append-only — no row is ever updated — and the fold
is a read-time projection.
```

New entry under `## Storage`, after `host-supplied-fields-earn-the-query-surface`:

```markdown
### a-driver-may-know-the-domain

A driver holds whatever answers the core's question in one round-trip; the core
holds what is a pure fold over rows already read.

**Blocks:** "record-shaped, not domain-shaped" as the dividing line — keeping
llmbroker's vocabulary out of driver bodies.
**Why:** that line never held. The journal operations only ever receive one table,
and the driver protocol already states the append-only rule and what a quality row
is. It also priced a read wrong: obeying it meant fetching calls and ratings
separately and matching them above the driver — two round-trips and a list of ids
on the wire, or a scan repeated in the file store, to avoid a correlated lookup
each backend performs natively. The line that does work is whether two correct
backends could answer differently: a projection cannot be disagreed about, a
threshold or a window can, so those stay in the core. Table and column names still
come from the one declarative spec, which is what keeps a rename to a single edit.
```

## Semantics to get right

**The filters narrow calls, never ratings.** `scope`, `operation`, `trace_id`,
`call_id` and `since` apply to the call row only. A rating carries no operation and
no trace, its own `scope` is not consulted, and it is not bounded by `since` — a
rating written after the window still attaches to a call inside it. Stated once
here because all four implementations must agree.

**Newest rating wins.** Ratings are append-only with no dedup; the projection takes
the newest per call, by `called_at`.

**Answering means `kind="call"` and `status is CallStatus.OK`.** A rate-limited
attempt produced nothing to judge. This lives inside llmbroker.

**The quality row stores no model and no operation.** They are NULL. The row is
`id` (fresh uuid, the table key), `kind="quality"`, `call_id`, `quality_score`,
`scope`, `called_at`.

## Work order

### 1. `Call` stops being a union — `src/llmbroker/models.py:201`

Remove `kind`, `call_id`, `quality_score`. Add `score: float | None = None`,
populated on reads only. The remaining fields are exactly one call attempt.

`kind` survives as a column literal, set by the stores, never on the type. Add to
`journal_policy.py`:

```python
KIND_CALL = "call"
KIND_QUALITY = "quality"


def quality_row(call_id: str, score: float, scope: str | None) -> dict[str, object]:
    """The appended rating: names the call, carries no model and no operation."""
```

Delete `quality_call()` (`journal_policy.py:14`) — no rating is materialized as a
`Call` anywhere any more.

### 2. `Driver.journal_view` replaces `Driver.recent` — `backends/driver.py:37`

```python
async def journal_view(
    self,
    limit: int,
    match: Row | None = None,
    since: datetime | None = None,
) -> list[Row]:
    """Newest-first call rows, each with a ``score`` key holding its newest rating's
    value or ``None``. ``match`` and ``since`` narrow the call rows only."""
    ...
```

No `table` parameter: it only ever received `"calls"`. `append` and `purge` keep
theirs — renaming them is churn with no gain.

Replace the module docstring (`backends/driver.py:1`), inside the 3-line cap:

```python
"""The per-DB storage contract: one round-trip per read, and no logic two correct
backends could answer differently — that stays in ``backends.ports``. Table and
column names come from ``backends.spec``, never spelled out in a driver body."""
```

### 3. The three drivers

**sqlite** (`sqlite/driver.py:245`) and **postgres** (`postgres/driver.py:181`) —
the same SQL modulo placeholder style. Column list and table name still built from
`spec`; the two kind literals imported from `journal_policy`:

```sql
SELECT c.<spec.columns...>,
       (SELECT q.quality_score FROM <spec.name> q
        WHERE q.kind = 'quality' AND q.call_id = c.id
        ORDER BY q.called_at DESC LIMIT 1) AS score
FROM <spec.name> c
WHERE c.kind = 'call' [AND <match conditions on c>] [AND c.called_at >= ?]
ORDER BY c.called_at DESC LIMIT ?
```

The correlated subquery is what makes "newest rating wins" free and keeps the row
count at exactly `limit`. Existing `match` handling (`None` value → `IS NULL`) is
unchanged, prefixed with `c.`.

**mongodb** (`mongodb/driver.py:130`) — one aggregation pipeline:

```python
[
    {"$match": {**query, "kind": KIND_CALL}},
    {"$sort": {"called_at": -1}},
    {"$limit": limit},
    {"$lookup": {
        "from": spec.name,
        "let": {"cid": "$id"},
        "pipeline": [
            {"$match": {"$expr": {"$and": [
                {"$eq": ["$kind", KIND_QUALITY]},
                {"$eq": ["$call_id", "$$cid"]},
            ]}}},
            {"$sort": {"called_at": -1}},
            {"$limit": 1},
        ],
        "as": "_rating",
    }},
    {"$addFields": {"score": {"$arrayElemAt": ["$_rating.quality_score", 0]}}},
]
```

`$arrayElemAt`, not `$first` — the latter is 4.4+. `_decode_doc` gains `score`
beside the spec columns.

### 4. The index — `backends/spec.py:73`

`indexes` becomes `(("llm_name",), ("called_at",), ("trace_id",), ("call_id",))`.
Load-bearing: the correlated subquery runs once per returned row.

No `SCHEMA_VERSION` bump. The column shape does not change, and every driver
re-issues the whole index DDL on a fresh process — established and pinned by
`test_sqlite_existing_database_gains_a_new_index_without_a_version_bump`.

### 5. `DriverStore` — `backends/ports.py`

- `_call_to_row` (line 85) sets `"kind": KIND_CALL` and leaves `call_id` /
  `quality_score` `None`; it no longer reads them off `Call`.
- `_row_to_call` (line 107) reads `score` from the row, drops `kind` /
  `call_id` / `quality_score`.
- `record_quality` (line 155) becomes
  `async def record_quality(self, call_id: str, score: float, *, scope: str | None = None)`,
  appending `quality_row(...)` directly.
- `calls` (line 166) drops `kind`, calls `self._driver.journal_view(...)`. Its
  `# noqa: PLR0913` should be removable — recount after the edit.

### 6. `FileStore` — `standalone/store.py`

`_read_tail` (line 130) becomes a single reverse pass with a pending map. Ratings
are newer than their calls, so a rating is always met before the call it rates:

```python
pending: dict[str, float] = {}   # call id -> newest score seen
...
raw = json.loads(stripped)
if raw.get("kind") == KIND_QUALITY:
    cid = raw.get("call_id")
    if cid is not None and raw.get("quality_score") is not None:
        pending.setdefault(cid, raw["quality_score"])   # newest-first: first wins
    continue
call = _call_from_jsonable(raw)
if <match / since fail>:
    continue
result.append(replace(call, score=pending.pop(call.id, None)))
```

Filters and `since` are checked on the call only — a rating is never skipped by
them. `record_quality` writes `quality_row(...)` as a JSONL line directly, with no
`Call` round-trip. `InMemoryStore.record_quality` (line 29) takes the new
signature.

### 7. The core loses its three kind branches

- `stats.py:31` — delete `if row.kind != "call": continue`; update the docstring
  and the doctest, which constructs `Call` positionally.
- `learning.py:44` (`budget_bounds_from_calls`) — delete the `kind` half of the
  condition.
- `learning.py:121` (`_apply_scores_and_metrics`) — becomes:

```python
for row in rows:
    if row.score is None:
        continue
    bucket = scores.setdefault((row.llm_name, row.operation), [])
    if len(bucket) < self._opt.quality_window:
        bucket.append(row.score)
```

`relearn()` (line 105) is otherwise unchanged: still one read, now of the view,
and its 300 rows are now 300 calls rather than calls and ratings sharing the
budget.

### 8. The public write form

`AsyncLLMs.record_quality` (`broker/llms.py:217`):

```python
async def record_quality(
    self,
    score: float,
    *,
    call_id: str | None = None,
    trace_id: str | None = None,
) -> None:
```

Exactly one of the two is required; zero or both → `ValueError` naming them. The
positional `(llm_name, operation, score)` form is removed outright — no shim, per
the architecture note in `CLAUDE.md`.

Body: resolve through `self.calls(...)` (the scoped read, so a scoped caller
resolves its own rows) with a fixed row bound; a read that comes back full logs a
warning naming the key and rates what it found. Keep the `OK` rows, write one
quality row per resolved call, and fold each forward into the learner with the
model and operation from the resolved row. Nothing resolved → `UnknownCallError`.

`exceptions.py` — add `UnknownCallError(LLMBrokerError)`, exported from
`llmbroker/__init__.py` beside its siblings. A non-queryable store keeps surfacing
the `TypeError` `calls()` already raises; do not catch and re-wrap it.

`AsyncResult.record_quality` (`broker/result.py:56`) keeps its fast path — it
holds `call_id`, `llm_name` and `operation` already, so it writes and folds
without a read.

Pass-throughs, all losing the same three positional parameters:
`broker/broker.py:411`, `sync.py:160`, `sync.py:287`. `sync.py:73`
(`Result.record_quality`) is already `(score)` and only needs its target checked.

### 9. Read pass-throughs lose `kind`

`protocols/store.py:32`, `backends/ports.py:166`, `standalone/store.py:167`,
`broker/llms.py:244`, `broker/broker.py:462`, `sync.py:170`, `sync.py:313`.
`llms.py:278` (`stats`) drops `kind="call"` from its own call.

### 10. Docs — both languages in the same batch

`docs/src/en/server.md` + `docs/src/ru/server.md`:

- "Call journal" intro (en:246, ru:249) — drop "two kinds of record … interleaved
  in one stream" and the `kind=` example. State one row per call with `score`.
- "Tracing one request" (en:263, ru:266) — delete the naming-trap paragraph;
  `call_id=` now has one meaning.
- The rating section — the new `record_quality` signature, and that a rating
  reaches the pool through the call it names.

`docs/src/en/usage.md` "Quality rating" (line 316) + `docs/src/ru/usage.md` (line
326) — the delayed-rating half (en:332–343, ru:343–354) currently teaches
persisting `reply.llm_name` and `reply.operation` and calling
`llms.record_quality(llm_name, operation, 0.0)`. That advice becomes wrong, not
merely dated: rewrite it around the two key forms — pass your own id as `trace_id`
at call time and rate by it later, or persist `reply.call_id`. Say that one
`trace_id` covering several calls rates them all, and that failed attempts are not
rated.

Also state, in both languages, that a scoped caller must rate through the same
scope (`broker.for_scope(x).record_quality(...)`): the scope comes from the caller
object, not from the key, so a rating sent through the bare broker lands unscoped.

The `trace_id` paragraph itself (en:283, ru:292) stands unchanged.

## Specs

- **`invariants.md` #1** — currently "A quality rating is a separate self-contained
  record, never joined to the call it rates." The second half is now false.
  Rewrite: the journal is append-only, no row is ever updated, and a rating is its
  own appended row naming the call it rates.
- **`decisions.md`** — the two blocks above, verbatim;
  `self-contained-quality-records` replaced in place.
- **`rules/backends.md`** "The read path" (line ~200) — one read form for a host
  (one row per call, with its score) and one aggregate; the kind filter and the
  paragraph explaining why it matters both go.
- **`rules/selection.md`** lines 69–73 — currently "a host that persists the
  rating identity can record the verdict days or months later … self-contained
  quality records are what makes an arbitrarily late rating safe, since retention
  may already have purged the original call row." That is now false in both
  halves. Rewrite: a rating lands for as long as the journal still holds the call
  it names, and it counts toward that call's model and operation; past retention
  there is no call left to name. Keep the surrounding demotion rules untouched.
- `mission.md` — re-read "Quality is the host's verdict" against the code. It
  states intent, not mechanism, so it likely needs nothing; confirm rather than
  assume.

## Tests

Twenty files touch `kind=`/`record_quality` today: `test_store_backends.py`,
`test_store.py`, `test_store_traffic.py`, `test_stats.py`, `test_learning.py`,
`test_file_learning.py`, `test_optimizer_integration.py`, `test_score_validation.py`,
`test_callers.py`, `test_broker.py`, `test_broker_disable.py`, `test_sync.py`,
`test_router*.py`, `test_budget_ordering.py`, `test_pool*.py`,
`test_wait_budget.py`, `test_driver_conformance.py`. Most need only the new
`record_quality` call shape and `Call` without `kind`.

New, in `test_store_backends.py` (parametrized over file / sqlite / postgres /
mongodb via `queryable_store`, so each runs on all four):

- `test_calls_returns_one_row_per_call_with_its_score`
- `test_calls_returns_none_score_for_an_unrated_call`
- `test_calls_never_returns_a_rating_as_its_own_row`
- `test_calls_newest_rating_wins_when_a_call_is_rated_twice`
- `test_calls_limit_counts_calls_not_journal_rows` — N ratings between calls do
  not eat the page. Fails before this change.
- `test_calls_score_attaches_though_the_rating_is_outside_since` — the rating is
  newer than the window bound; the call is inside it.
- `test_calls_score_attaches_though_the_rating_carries_another_scope`
- `test_calls_operation_and_trace_filters_do_not_drop_a_rated_call` — a rating
  carries neither, so filtering must not lose the score.

`test_driver_conformance.py` — `journal_view` in place of `recent`, with the same
four-backend parametrization.

`test_schema_migration.py` — `test_sqlite_ensure_schema_creates_the_call_id_index`
and the mongodb mirror.

`test_learning.py` — the rebuild fills the quality window from view rows;
`budget_bounds_from_calls` and `stats_from_calls` over rows that all carry
`score`, some `None`.

New, for the write form (`test_score_validation.py` or a new
`test_rating_by_call.py`):

- `test_record_quality_by_call_id_rates_that_model`
- `test_record_quality_by_trace_id_rates_every_answering_attempt`
- `test_record_quality_by_trace_id_ignores_the_attempts_that_failed`
- `test_record_quality_raises_when_nothing_resolves` — `UnknownCallError`
- `test_record_quality_requires_exactly_one_key` — zero and both → `ValueError`
- `test_record_quality_warns_when_the_resolution_read_comes_back_full`
- `test_result_record_quality_needs_no_read`
- `test_scoped_caller_resolves_and_writes_its_own_scope` — two scopes hold rows
  under one trace; each rates only its own.
- `test_record_quality_on_a_non_queryable_store_raises_typeerror` —
  `InMemoryStore`, surfacing the message `calls()` already raises.

In `tests/test_score_validation.py`, extend the existing broker-level pair (lines
35, 45) to a key form: the score is validated **before** any lookup, so a bad
score raises `ValueError` rather than `UnknownCallError`.

No `pytest.skip` / `importorskip` anywhere; postgres and mongodb come up under
testcontainers, so Docker must be running.

## Gate

`invoke pre` clean (ruff, ruff-format, pyrefly, docstring cap) and
`python -m pytest` reporting `N passed` with zero failures, errors or skips.

Version bump (`invoke ver-feature`) is the maintainer's and is skipped here.

"""Backend-parametrized tests for the QueryableStoreProtocol (file, sqlite, postgres,
mongodb)."""

import asyncio
from dataclasses import fields
from datetime import UTC, datetime, timedelta, timezone

import pytest

from llmbroker.broker.learning import metrics_from_calls
from llmbroker.broker.stats import stats_from_calls
from llmbroker.journal_policy import PurgeClock
from llmbroker.models import Call, CallStatus, Usage

_BASE = datetime(2030, 1, 1, tzinfo=UTC)
# A rating is stamped as it is written, so a call it rates must be older than the
# run itself — the file store folds in one newest-first pass over its day files.
_NOW = datetime.now(UTC)


def _call(
    call_id="c1",
    llm_name="p1",
    scope=None,
    operation=None,
    status=CallStatus.OK,
    trace_id=None,
    **kw,
):
    return Call(
        id=call_id,
        llm_name=llm_name,
        operation=operation,
        trace_id=trace_id,
        status=status,
        scope=scope,
        **kw,
    )


async def test_record_and_calls(queryable_store):
    await queryable_store.record(_call("c1"))
    calls = await queryable_store.calls(limit=10)
    assert len(calls) == 1
    assert calls[0].id == "c1"


async def test_calls_respects_limit(queryable_store):
    for i in range(5):
        await queryable_store.record(_call(f"c{i}"))
    calls = await queryable_store.calls(limit=3)
    assert len(calls) == 3


async def test_calls_scope_filter(queryable_store):
    await queryable_store.record(_call("ca", scope="alice"))
    await queryable_store.record(_call("cb", scope="bob"))
    alice_calls = await queryable_store.calls(limit=10, scope="alice")
    assert len(alice_calls) == 1
    assert alice_calls[0].id == "ca"


async def test_calls_unscoped_read_spans_all_scopes(queryable_store):
    """The rebuild's tail read is unscoped — learning is global."""
    await queryable_store.record(_call("ca", scope="alice"))
    await queryable_store.record(_call("cb", scope="bob"))
    all_calls = await queryable_store.calls(limit=10)
    assert {c.id for c in all_calls} == {"ca", "cb"}


async def test_calls_roundtrips_usage_with_extra(queryable_store):
    usage = Usage(prompt_tokens=10, completion_tokens=20, total_tokens=30, extra={"cached": 5})
    await queryable_store.record(_call(usage=usage))
    calls = await queryable_store.calls(limit=1)
    u = calls[0].usage
    assert u is not None
    assert u.prompt_tokens == 10
    assert u.extra == {"cached": 5}


async def test_calls_none_usage_roundtrips_as_none(queryable_store):
    await queryable_store.record(_call())
    calls = await queryable_store.calls(limit=1)
    assert calls[0].usage is None


async def test_calls_returns_one_row_per_call_with_its_score(queryable_store):
    """A rating is appended as its own row and comes back folded onto the call it
    names — the journal is never updated in place."""
    await queryable_store.record(_call("c1", ts=_NOW))
    await queryable_store.record_quality("c1", 0.8)

    rows = await queryable_store.calls(limit=10)
    assert [r.id for r in rows] == ["c1"]
    assert rows[0].score == pytest.approx(0.8)
    assert rows[0].status is CallStatus.OK


async def test_calls_returns_none_score_for_an_unrated_call(queryable_store):
    await queryable_store.record(_call("c1", ts=_NOW))
    (row,) = await queryable_store.calls(limit=10)
    assert row.score is None


async def test_calls_never_returns_a_rating_as_its_own_row(queryable_store):
    """Above the storage layer the two record kinds stop existing: a rating names a
    call, and ``call_id=`` still means that call's own id."""
    await queryable_store.record(_call("a", ts=_NOW))
    await queryable_store.record_quality("a", 0.8)
    assert [r.id for r in await queryable_store.calls(limit=10)] == ["a"]
    assert [r.id for r in await queryable_store.calls(limit=10, call_id="a")] == ["a"]


async def test_calls_newest_rating_wins_when_a_call_is_rated_twice(queryable_store):
    """Ratings are append-only with no dedup, so a host changing its mind must not
    vote twice — the projection takes the newest."""
    await queryable_store.record(_call("c1", ts=_NOW))
    await queryable_store.record_quality("c1", 0.1)
    await asyncio.sleep(0.01)  # BSON dates carry whole milliseconds only
    await queryable_store.record_quality("c1", 0.9)
    (row,) = await queryable_store.calls(limit=10)
    assert row.score == pytest.approx(0.9)


async def test_calls_limit_counts_calls_not_journal_rows(queryable_store):
    """A rated journal must not cost a host half its page."""
    for i in range(3):
        await queryable_store.record(_call(f"c{i}", ts=_NOW - timedelta(seconds=3 - i)))
    for i in range(3):
        await queryable_store.record_quality(f"c{i}", 0.5)
    rows = await queryable_store.calls(limit=3)
    assert {r.id for r in rows} == {"c0", "c1", "c2"}
    assert all(r.score == pytest.approx(0.5) for r in rows)


async def test_calls_score_attaches_though_the_rating_is_outside_since(queryable_store):
    """``since`` bounds the call row only: a rating written later than the window was
    formed still lands on the call inside it."""
    await queryable_store.record(_call("stale", ts=_NOW - timedelta(days=2)))
    await queryable_store.record(_call("fresh", ts=_NOW - timedelta(hours=1)))
    await queryable_store.record_quality("fresh", 0.7)
    rows = await queryable_store.calls(limit=10, since=_NOW - timedelta(days=1))
    assert [(r.id, r.score) for r in rows] == [("fresh", pytest.approx(0.7))]


async def test_calls_score_attaches_though_the_rating_carries_another_scope(queryable_store):
    """A rating carries its writer's scope, and the read never consults it — the
    filters narrow the calls."""
    await queryable_store.record(_call("c1", scope="alice", ts=_NOW))
    await queryable_store.record_quality("c1", 0.6, scope="bob")
    (row,) = await queryable_store.calls(limit=10, scope="alice")
    assert row.score == pytest.approx(0.6)


async def test_calls_operation_and_trace_filters_do_not_drop_a_rated_call(queryable_store):
    """A rating carries neither an operation nor a trace, so narrowing by them must
    not lose the score."""
    await queryable_store.record(
        _call("c1", operation="summarize", trace_id="req-1", ts=_NOW),
    )
    await queryable_store.record_quality("c1", 0.4)
    rows = await queryable_store.calls(limit=10, operation="summarize", trace_id="req-1")
    assert [(r.id, r.score) for r in rows] == [("c1", pytest.approx(0.4))]


async def test_retention_purges_old_calls_via_maybe_purge(queryable_store):
    """Retention purge runs internally on write, debounced to at most once per hour in
    normal operation — verified directly here via the private purge hook.

    A negative day (not just seconds) margin so the file backend's whole-day-file
    purge also sees today's file as expired (it truncates the cutoff to a date)."""
    await queryable_store.record(_call("old"))
    queryable_store._retention = timedelta(days=-1)  # everything recorded is now "old"
    queryable_store._purge_clock = PurgeClock()  # a fresh clock is due immediately
    await queryable_store._maybe_purge()
    assert await queryable_store.calls(limit=10) == []


async def test_retention_purge_is_debounced(queryable_store):
    """A purge that just ran (via record()'s own _maybe_purge call) does not re-run
    within the debounce window, even if retention would otherwise delete the row."""
    await queryable_store.record(_call("c1"))
    queryable_store._retention = timedelta(seconds=-1)
    await queryable_store._maybe_purge()
    assert len(await queryable_store.calls(limit=10)) == 1


async def test_metrics_counts_calls_per_llm(queryable_store):
    await queryable_store.record(_call("c1", llm_name="llm1"))
    await queryable_store.record(_call("c2", llm_name="llm1"))
    await queryable_store.record(_call("c3", llm_name="llm2"))
    m = metrics_from_calls(await queryable_store.calls(limit=100))
    assert m["llm1"].call_count == 2
    assert m["llm2"].call_count == 1


# ── Windowed / narrowed journal reads ───────────────────────────────────────


async def test_calls_since_bounds_the_window(queryable_store):
    await queryable_store.record(_call("old", ts=_BASE))
    await queryable_store.record(_call("new", ts=_BASE + timedelta(days=2)))
    rows = await queryable_store.calls(limit=10, since=_BASE + timedelta(days=1))
    assert [c.id for c in rows] == ["new"]


async def test_calls_since_is_inclusive_at_the_bound(queryable_store):
    await queryable_store.record(_call("exactly-at", ts=_BASE))
    rows = await queryable_store.calls(limit=10, since=_BASE)
    assert [c.id for c in rows] == ["exactly-at"]


async def test_calls_operation_filter_keeps_only_that_operation(queryable_store):
    await queryable_store.record(_call("summ", operation="summarize", ts=_BASE))
    await queryable_store.record(_call("judge", operation="llmbroker.judge", ts=_BASE))
    rows = await queryable_store.calls(limit=10, operation="summarize")
    assert [c.id for c in rows] == ["summ"]


async def test_calls_combines_since_and_operation(queryable_store):
    """Both must be load-bearing: every decoy below is excluded by exactly one of
    them, so dropping either argument fails the assertion."""
    in_window = _BASE + timedelta(days=2)
    await queryable_store.record(_call("old-summ", operation="summarize", ts=_BASE))
    await queryable_store.record(_call("new-other", operation="translate", ts=in_window))
    await queryable_store.record(_call("new-summ", operation="summarize", ts=in_window))
    rows = await queryable_store.calls(
        limit=10,
        since=_BASE + timedelta(days=1),
        operation="summarize",
    )
    assert [c.id for c in rows] == ["new-summ"]


async def test_calls_trace_id_filter_keeps_only_that_trace(queryable_store):
    await queryable_store.record(_call("mine", trace_id="req-1", ts=_BASE))
    await queryable_store.record(_call("theirs", trace_id="req-2", ts=_BASE))
    rows = await queryable_store.calls(limit=10, trace_id="req-1")
    assert [c.id for c in rows] == ["mine"]


async def test_calls_trace_id_spans_a_failover_burst(queryable_store):
    """The question the filter exists for: every attempt one request made, including
    the ones that failed before another model answered."""
    await queryable_store.record(
        _call("a1", trace_id="req-1", status=CallStatus.RATE_LIMITED, ts=_BASE),
    )
    await queryable_store.record(
        _call("a2", trace_id="req-1", status=CallStatus.RATE_LIMITED, ts=_BASE),
    )
    await queryable_store.record(_call("a3", trace_id="req-1", status=CallStatus.OK, ts=_BASE))
    rows = await queryable_store.calls(limit=10, trace_id="req-1")
    assert {c.id for c in rows} == {"a1", "a2", "a3"}
    assert [c.id for c in rows if c.status is CallStatus.OK] == ["a3"]


async def test_calls_trace_id_limit_bounds_matching_rows_not_scanned(queryable_store):
    """``limit`` caps the rows returned, not the rows looked at: a trace buried under
    newer traffic still comes back whole rather than being pushed off the tail."""
    await queryable_store.record(_call("t1a", trace_id="req-1", ts=_BASE))
    await queryable_store.record(_call("t1b", trace_id="req-1", ts=_BASE))
    newer = _BASE + timedelta(days=2)
    for i in range(4):
        await queryable_store.record(_call(f"t2{i}", trace_id="req-2", ts=newer))
    rows = await queryable_store.calls(limit=2, trace_id="req-1")
    assert {c.id for c in rows} == {"t1a", "t1b"}


async def test_calls_call_id_selects_one_attempt(queryable_store):
    await queryable_store.record(_call("first", trace_id="req-1", ts=_BASE))
    await queryable_store.record(_call("second", trace_id="req-1", ts=_BASE))
    rows = await queryable_store.calls(limit=10, call_id="second")
    assert [c.id for c in rows] == ["second"]


async def test_calls_combines_the_id_filters_with_since_and_operation(queryable_store):
    """Every decoy is excluded by exactly one argument, so dropping any of them
    fails the assertion."""
    in_window = _BASE + timedelta(days=2)
    await queryable_store.record(_call("old-summ", trace_id="req-1", operation="summ", ts=_BASE))
    await queryable_store.record(
        _call("new-other", trace_id="req-1", operation="translate", ts=in_window),
    )
    await queryable_store.record(
        _call("other-trace", trace_id="req-2", operation="summ", ts=in_window),
    )
    await queryable_store.record(
        _call("new-summ", trace_id="req-1", operation="summ", ts=in_window)
    )

    narrowed = {
        "limit": 10,
        "since": _BASE + timedelta(days=1),
        "operation": "summ",
        "trace_id": "req-1",
    }
    assert [c.id for c in await queryable_store.calls(**narrowed)] == ["new-summ"]
    assert [c.id for c in await queryable_store.calls(**narrowed, call_id="new-summ")] == [
        "new-summ",
    ]
    assert await queryable_store.calls(**narrowed, call_id="old-summ") == []


async def test_calls_unset_id_filters_return_every_row(queryable_store):
    """Both share the operation filter's semantics: unset means do not filter, so a
    row carrying no trace_id at all is not excluded by leaving the filter off."""
    await queryable_store.record(_call("traced", trace_id="req-1", ts=_BASE))
    await queryable_store.record(_call("untraced", ts=_BASE))
    rows = await queryable_store.calls(limit=10, trace_id=None, call_id=None)
    assert {c.id for c in rows} == {"traced", "untraced"}


async def test_stats_over_a_window_counts_only_that_operation(queryable_store):
    """The property that keeps a host's per-model numbers free of broker-internal
    traffic journaled under its own operation."""
    await queryable_store.record(_call("stale", operation="summarize", ts=_BASE))
    in_window = _BASE + timedelta(days=2)
    await queryable_store.record(_call("host1", operation="summarize", ts=in_window))
    await queryable_store.record(
        _call("host2", operation="summarize", ts=in_window, status=CallStatus.ERROR),
    )
    await queryable_store.record(_call("internal", operation="llmbroker.judge", ts=in_window))
    rows = await queryable_store.calls(
        limit=100,
        operation="summarize",
        since=_BASE + timedelta(days=1),
    )
    stats = stats_from_calls(rows)["p1"]
    assert stats.total == 2
    assert stats.by_status == {CallStatus.OK: 1, CallStatus.ERROR: 1}


# ── The since/limit contract, uniform across every backend ──────────────────


async def test_calls_rejects_naive_since(queryable_store):
    """Refused rather than guessed at: assuming UTC would shift the window by the
    caller's offset on some backends and raise on others."""
    with pytest.raises(ValueError, match="timezone-aware"):
        await queryable_store.calls(limit=10, since=datetime(2030, 1, 1))  # noqa: DTZ001


async def test_calls_since_in_another_offset_selects_the_same_rows(queryable_store):
    """The bound denotes an instant, not a printed wall clock."""
    await queryable_store.record(_call("c1", ts=_BASE))
    east = _BASE.astimezone(timezone(timedelta(hours=5)))
    assert [c.id for c in await queryable_store.calls(limit=10, since=east)] == ["c1"]


async def test_record_rejects_naive_ts(queryable_store):
    """Symmetric with the read bound: the journal is ordered and windowed by ``ts``,
    so a naive one is refused at the write boundary rather than stored ambiguously."""
    with pytest.raises(ValueError, match="timezone-aware"):
        await queryable_store.record(_call("naive", ts=datetime(2030, 1, 1)))  # noqa: DTZ001


async def test_record_normalizes_ts_offset_to_utc(queryable_store):
    """A row written in another offset is the same instant on the way back out."""
    eastern = datetime(2030, 1, 1, 13, tzinfo=timezone(timedelta(hours=5)))
    await queryable_store.record(_call("eastern", ts=eastern))
    (row,) = await queryable_store.calls(limit=10)
    assert row.ts == eastern
    assert row.ts.utcoffset() == timedelta(0)


@pytest.mark.parametrize("limit", [0, -1])
async def test_calls_rejects_non_positive_limit(queryable_store, limit):
    """pymongo reads limit=0 as *no limit*, so a caller's shrinking budget would
    silently become an unbounded scan of the whole journal on one backend."""
    await queryable_store.record(_call("c1", ts=_BASE))
    with pytest.raises(ValueError, match="limit must be"):
        await queryable_store.calls(limit=limit)


# ── Lossless persistence (invariant 8) ──────────────────────────────────────

# Distinguishable, non-empty value per journal field. Populating every field at
# once makes this a storage-fidelity fixture, not a semantically valid record.
_FIELD_SAMPLES = {
    "id": "rt-1",
    "llm_name": "p1",
    "operation": "summarize",
    "trace_id": "trace-abc",
    "status": CallStatus.ERROR,
    "ts": _BASE,
    "http_status": 503,
    "latency_ms": 1234,
    "error_detail": "provider said no",
    "usage": Usage(prompt_tokens=1, completion_tokens=2, total_tokens=3, extra={"cached": 4}),
    "scope": "tenant-a",
    "cooldown_until": _BASE + timedelta(minutes=30),
    "budget_ms": 1500,
}


# Filled by the read, never written: a rating is its own appended row.
_READ_ONLY_FIELDS = {"score"}


def _fully_populated_call() -> Call:
    declared = {f.name for f in fields(Call)}
    unsampled = declared - _FIELD_SAMPLES.keys() - _READ_ONLY_FIELDS
    assert not unsampled, (
        f"journal field(s) {sorted(unsampled)} have no sample value — add one, or the "
        f"lossless-persistence guarantee silently stops covering them"
    )
    blank = {k for k, v in _FIELD_SAMPLES.items() if v is None or v == ""}
    assert not blank, f"sample value for {sorted(blank)} is empty, so it proves nothing"
    return Call(**_FIELD_SAMPLES)  # type: ignore[arg-type]


async def test_every_journal_field_survives_the_store(queryable_store):
    """A backend may not persist a subset of the row it was handed: a dropped field
    degrades selection with a green gate rather than raising."""
    original = _fully_populated_call()
    await queryable_store.record(original)
    (restored,) = await queryable_store.calls(limit=10)
    assert restored == original

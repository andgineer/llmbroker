"""Backend-parametrized tests for the QueryableStoreProtocol (sqlite, postgres, mongodb)."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from llmbroker.broker.learning import metrics_from_calls
from llmbroker.broker.stats import stats_from_calls
from llmbroker.models import Call, CallStatus, Usage

_BASE = datetime(2030, 1, 1, tzinfo=UTC)


def _call(call_id="c1", llm_name="p1", scope=None, operation=None, status=CallStatus.OK, **kw):
    return Call(
        id=call_id,
        llm_name=llm_name,
        operation=operation,
        trace_id=None,
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


async def test_record_quality_appends_self_contained_row(queryable_store):
    """record_quality appends its own journal row — never updates the call row."""
    await queryable_store.record(_call("c1"))
    await queryable_store.record_quality("p1", "summarize", 0.8, call_id="c1")

    rows = await queryable_store.calls(limit=10)
    assert len(rows) == 2
    quality_rows = [r for r in rows if r.kind == "quality"]
    assert len(quality_rows) == 1
    assert quality_rows[0].quality_score == pytest.approx(0.8)
    assert quality_rows[0].llm_name == "p1"
    assert quality_rows[0].operation == "summarize"
    assert quality_rows[0].call_id == "c1"
    assert quality_rows[0].status is None

    call_rows = [r for r in rows if r.kind == "call"]
    assert call_rows[0].quality_score is None


async def test_retention_purges_old_calls_via_maybe_purge(queryable_store):
    """Retention purge runs internally on write, debounced to at most once per hour in
    normal operation — verified directly here via the private purge hook.

    A negative day (not just seconds) margin so the file backend's whole-day-file
    purge also sees today's file as expired (it truncates the cutoff to a date)."""
    await queryable_store.record(_call("old"))
    queryable_store._retention = timedelta(days=-1)  # everything recorded is now "old"
    queryable_store._last_purge = float("-inf")  # force the debounce open
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


async def test_calls_kind_filter_drops_quality_records(queryable_store):
    """Without it a host computing a failure ratio silently gets quality rows —
    which carry status=None by construction — in its denominator."""
    await queryable_store.record(_call("c1", ts=_BASE))
    await queryable_store.record_quality("p1", "summarize", 0.9)
    assert {c.id for c in await queryable_store.calls(limit=10, kind="call")} == {"c1"}
    quality = await queryable_store.calls(limit=10, kind="quality")
    assert [c.kind for c in quality] == ["quality"]


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


async def test_calls_combines_since_kind_and_operation(queryable_store):
    """Each of the three must be load-bearing: every decoy below is excluded by
    exactly one of them, so dropping any argument fails the assertion."""
    in_window = _BASE + timedelta(days=2)
    await queryable_store.record(_call("old-summ", operation="summarize", ts=_BASE))
    await queryable_store.record(_call("new-other", operation="translate", ts=in_window))
    await queryable_store.record(
        _call("new-summ-quality", operation="summarize", ts=in_window, status=None, kind="quality"),
    )
    await queryable_store.record(_call("new-summ", operation="summarize", ts=in_window))
    rows = await queryable_store.calls(
        limit=10,
        since=_BASE + timedelta(days=1),
        kind="call",
        operation="summarize",
    )
    assert [c.id for c in rows] == ["new-summ"]


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

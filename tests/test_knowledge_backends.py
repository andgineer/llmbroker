"""Backend-parametrized tests for the QueryableKnowledgeProtocol (sqlite, postgres, mongodb)."""

from datetime import UTC, datetime, timedelta

import pytest

from llmbroker.models import Call, CallStatus, Usage


def _call(call_id="c1", llm_name="p1", scope=None, **kw):
    return Call(
        id=call_id,
        llm_name=llm_name,
        operation=None,
        trace_id=None,
        status=CallStatus.OK,
        scope=scope,
        **kw,
    )


async def test_record_and_calls(queryable_knowledge):
    await queryable_knowledge.record(_call("c1"))
    calls = await queryable_knowledge.calls(limit=10)
    assert len(calls) == 1
    assert calls[0].id == "c1"


async def test_calls_respects_limit(queryable_knowledge):
    for i in range(5):
        await queryable_knowledge.record(_call(f"c{i}"))
    calls = await queryable_knowledge.calls(limit=3)
    assert len(calls) == 3


async def test_calls_scope_filter(queryable_knowledge):
    await queryable_knowledge.record(_call("ca", scope="alice"))
    await queryable_knowledge.record(_call("cb", scope="bob"))
    alice_calls = await queryable_knowledge.calls(limit=10, scope="alice")
    assert len(alice_calls) == 1
    assert alice_calls[0].id == "ca"


async def test_calls_unscoped_read_spans_all_scopes(queryable_knowledge):
    """The rebuild's tail read is unscoped — learning is global."""
    await queryable_knowledge.record(_call("ca", scope="alice"))
    await queryable_knowledge.record(_call("cb", scope="bob"))
    all_calls = await queryable_knowledge.calls(limit=10)
    assert {c.id for c in all_calls} == {"ca", "cb"}


async def test_calls_roundtrips_usage_with_extra(queryable_knowledge):
    usage = Usage(prompt_tokens=10, completion_tokens=20, total_tokens=30, extra={"cached": 5})
    await queryable_knowledge.record(_call(usage=usage))
    calls = await queryable_knowledge.calls(limit=1)
    u = calls[0].usage
    assert u is not None
    assert u.prompt_tokens == 10
    assert u.extra == {"cached": 5}


async def test_calls_none_usage_roundtrips_as_none(queryable_knowledge):
    await queryable_knowledge.record(_call())
    calls = await queryable_knowledge.calls(limit=1)
    assert calls[0].usage is None


async def test_record_quality_appends_self_contained_row(queryable_knowledge):
    """record_quality appends its own journal row — never updates the call row."""
    await queryable_knowledge.record(_call("c1"))
    await queryable_knowledge.record_quality("p1", "summarize", 0.8, call_id="c1")

    rows = await queryable_knowledge.calls(limit=10)
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


async def test_retention_purges_old_calls_via_maybe_purge(queryable_knowledge):
    """Retention purge runs internally on write, debounced to at most once per hour in
    normal operation — verified directly here via the private purge hook."""
    await queryable_knowledge.record(_call("old"))
    queryable_knowledge._retention = timedelta(seconds=-1)  # everything recorded is now "old"
    queryable_knowledge._last_purge = float("-inf")  # force the debounce open
    await queryable_knowledge._maybe_purge()
    assert await queryable_knowledge.calls(limit=10) == []


async def test_retention_purge_is_debounced(queryable_knowledge):
    """A purge that just ran (via record()'s own _maybe_purge call) does not re-run
    within the debounce window, even if retention would otherwise delete the row."""
    await queryable_knowledge.record(_call("c1"))
    queryable_knowledge._retention = timedelta(seconds=-1)
    await queryable_knowledge._maybe_purge()
    assert len(await queryable_knowledge.calls(limit=10)) == 1


async def test_metrics_counts_calls_per_llm(queryable_knowledge):
    await queryable_knowledge.record(_call("c1", llm_name="llm1"))
    await queryable_knowledge.record(_call("c2", llm_name="llm1"))
    await queryable_knowledge.record(_call("c3", llm_name="llm2"))
    m = await queryable_knowledge.metrics()
    assert m["llm1"].call_count == 2
    assert m["llm2"].call_count == 1


async def test_metrics_since_filters_past_calls(queryable_knowledge):
    await queryable_knowledge.record(_call())
    m = await queryable_knowledge.metrics(since=datetime.now(UTC) + timedelta(seconds=5))
    assert m == {}


async def test_metrics_user_id_scoping(queryable_knowledge):
    await queryable_knowledge.record(_call("ca", llm_name="llm1", scope="alice"))
    await queryable_knowledge.record(_call("cb", llm_name="llm1", scope="bob"))
    m_alice = await queryable_knowledge.metrics(user_id="alice")
    m_bob = await queryable_knowledge.metrics(user_id="bob")
    m_unscoped = await queryable_knowledge.metrics()
    assert m_alice["llm1"].call_count == 1
    assert m_bob["llm1"].call_count == 1
    assert m_unscoped == {}

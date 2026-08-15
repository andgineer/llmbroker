"""Unit tests for stats_from_calls: the per-model journal aggregate."""

from datetime import UTC, date, datetime, timedelta

import pytest

from llmbroker.broker.stats import stats_from_calls
from llmbroker.models import Call, CallStatus, to_utc

_BASE = datetime(2030, 1, 1, tzinfo=UTC)


def _call(call_id, llm_name="p1", status=CallStatus.OK, ts=None, operation=None, score=None):
    return Call(
        id=call_id,
        llm_name=llm_name,
        operation=operation,
        trace_id=None,
        status=status,
        ts=ts if ts is not None else _BASE,
        score=score,
    )


def test_empty_input_returns_empty_mapping():
    assert stats_from_calls([]) == {}


def test_to_utc_names_the_field_for_the_likeliest_misuses():
    """Passing a ``date`` for a datetime bound is the natural slip in an API whose
    whole job is validating the caller's bound — it must not surface as an
    AttributeError about ``tzinfo``."""
    with pytest.raises(TypeError, match="since must be a datetime, got date"):
        to_utc(date(2030, 1, 1), "since")
    with pytest.raises(ValueError, match="since must be timezone-aware"):
        to_utc(datetime(2030, 1, 1), "since")  # noqa: DTZ001
    assert to_utc(_BASE, "since") == _BASE


def test_counts_per_status():
    rows = [
        _call("c4", status=CallStatus.ERROR),
        _call("c3", status=CallStatus.OK),
        _call("c2", status=CallStatus.OK),
        _call("c1", status=CallStatus.RATE_LIMITED),
    ]
    stats = stats_from_calls(rows)["p1"]
    assert stats.total == 4
    assert stats.by_status == {
        CallStatus.ERROR: 1,
        CallStatus.OK: 2,
        CallStatus.RATE_LIMITED: 1,
    }


def test_by_status_holds_only_statuses_actually_seen():
    """A host reading "how many were not OK" subtracts from total rather than
    assuming every enum member is a key."""
    stats = stats_from_calls([_call("c1", status=CallStatus.OK)])["p1"]
    assert stats.by_status == {CallStatus.OK: 1}
    assert stats.total - stats.by_status.get(CallStatus.OK, 0) == 0


def test_a_rated_call_counts_once_like_any_other():
    """The score rides on the call row, so rating a call cannot inflate its count."""
    rows = [_call("c2", score=1.0), _call("c1")]
    stats = stats_from_calls(rows)["p1"]
    assert stats.total == 2
    assert stats.by_status == {CallStatus.OK: 2}


def test_first_last_and_last_status_from_newest_first_sequence():
    newest = _BASE + timedelta(days=2)
    oldest = _BASE
    rows = [
        _call("c3", status=CallStatus.ERROR, ts=newest),
        _call("c2", status=CallStatus.OK, ts=_BASE + timedelta(days=1)),
        _call("c1", status=CallStatus.OK, ts=oldest),
    ]
    stats = stats_from_calls(rows)["p1"]
    assert stats.last_at == newest
    assert stats.first_at == oldest
    assert stats.last_status is CallStatus.ERROR


def test_models_are_aggregated_independently():
    rows = [
        _call("c3", llm_name="p2", status=CallStatus.ERROR),
        _call("c2", llm_name="p1", status=CallStatus.OK),
        _call("c1", llm_name="p1", status=CallStatus.OK),
    ]
    stats = stats_from_calls(rows)
    assert stats["p1"].total == 2
    assert stats["p2"].total == 1
    assert stats["p2"].last_status is CallStatus.ERROR


def test_rows_without_status_are_counted_in_total_but_not_by_status():
    """``Call.status`` is optional in the type even though llmbroker's own writers
    always set it on a call row, so a third-party store can produce this shape: it
    must land in the denominator without inventing a status bucket."""
    rows = [_call("c2", status=None), _call("c1", status=CallStatus.OK)]
    stats = stats_from_calls(rows)["p1"]
    assert stats.total == 2
    assert stats.by_status == {CallStatus.OK: 1}

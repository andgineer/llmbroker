"""Tests for the broker's private in-memory live state."""

from datetime import UTC, datetime, timedelta

from llmbroker.models import LifecyclePhase
from llmbroker.state import InMemoryState


def test_fresh_llm_is_available():
    state = InMemoryState()
    s = state.get_state("p1")
    assert s.phase is LifecyclePhase.AVAILABLE
    assert s.cooldown_until is None
    assert s.fail_count == 0


def test_cooling_until_future_reports_cooling():
    state = InMemoryState()
    future = datetime.now(UTC) + timedelta(seconds=60)
    state.set_cooling("p1", future, fail_count=1)
    s = state.get_state("p1")
    assert s.phase is LifecyclePhase.COOLING
    assert s.cooldown_until == future
    assert s.fail_count == 1


def test_cooling_in_past_reports_available():
    state = InMemoryState()
    past = datetime.now(UTC) - timedelta(seconds=1)
    state.set_cooling("p1", past, fail_count=2)
    s = state.get_state("p1")
    assert s.phase is LifecyclePhase.AVAILABLE
    assert s.cooldown_until is None
    # fail_count is retained even after cooldown clears
    assert s.fail_count == 2


def test_clear_cooling_resets_to_available():
    state = InMemoryState()
    state.set_cooling("p1", datetime.now(UTC) + timedelta(seconds=60), fail_count=1)
    state.clear_cooling("p1")
    assert state.get_state("p1").phase is LifecyclePhase.AVAILABLE


def test_record_quality_fail_increments():
    state = InMemoryState()
    state.record_quality_fail("p1")
    state.record_quality_fail("p1")
    assert state.get_state("p1").fail_count == 2

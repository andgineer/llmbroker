"""Tests for LLMState/reconcile().

Phase-derivation and fail-count behavior over the broker's live per-LLM state
now lives in ``LLMPool`` (see ``tests/test_pool.py``) — the pool owns that
state directly on its slots.
"""

from datetime import UTC, datetime, timedelta

import pytest

from llmbroker.models import LifecyclePhase, LLMState, reconcile


# ── LLMState ⇄ dict round-trip and reconcile() ───────────────────────────────


def test_llm_state_to_dict_from_dict_round_trip_tz_aware_cooldown():
    future = datetime.now(UTC) + timedelta(seconds=120)
    state = LLMState(phase=LifecyclePhase.COOLING, cooldown_until=future, fail_count=2)
    assert LLMState.from_dict(state.to_dict()) == state


def test_llm_state_to_dict_from_dict_round_trip_none_cooldown():
    state = LLMState(phase=LifecyclePhase.AVAILABLE, cooldown_until=None, fail_count=0)
    assert LLMState.from_dict(state.to_dict()) == state


def test_llm_state_extra_key_preserved_round_trip():
    state = LLMState(fail_count=1, extra={"probe_attempts": 3})
    d = state.to_dict()
    assert d["probe_attempts"] == 3
    assert LLMState.from_dict(d) == state


def test_llm_state_from_dict_missing_keys_fall_back_to_defaults():
    assert LLMState.from_dict({}) == LLMState()


def test_llm_state_to_dict_extra_colliding_with_reserved_key_raises():
    state = LLMState(extra={"phase": "offline"})
    with pytest.raises(ValueError, match="reserved keys"):
        state.to_dict()


def test_reconcile_expired_cooldown_reports_available():
    now = datetime.now(UTC)
    past = now - timedelta(seconds=1)
    state = LLMState(phase=LifecyclePhase.COOLING, cooldown_until=past)
    result = reconcile(state, now)
    assert result.phase is LifecyclePhase.AVAILABLE
    assert result.cooldown_until is None


def test_reconcile_live_cooldown_reports_cooling():
    now = datetime.now(UTC)
    future = now + timedelta(seconds=60)
    state = LLMState(phase=LifecyclePhase.AVAILABLE, cooldown_until=future)
    result = reconcile(state, now)
    assert result.phase is LifecyclePhase.COOLING
    assert result.cooldown_until == future


def test_reconcile_no_cooldown_reports_available():
    now = datetime.now(UTC)
    state = LLMState(phase=LifecyclePhase.COOLING, cooldown_until=None)
    result = reconcile(state, now)
    assert result.phase is LifecyclePhase.AVAILABLE
    assert result.cooldown_until is None

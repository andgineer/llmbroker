"""Tests for LLMState/reconcile() and the SQLite state store.

Phase-derivation and fail-count behavior over the broker's live per-LLM state
now lives in ``LLMPool`` (see ``tests/test_pool.py``) — the pool owns that
state directly on its slots.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import llmbroker.sqlite
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


# ── SQLite StateStore tests ───────────────────────────────────────────────────


def test_sqlite_state_store_read_empty(tmp_path):
    store = llmbroker.sqlite.StateStore(str(tmp_path / "b.db"))
    assert asyncio.run(store.read()) == {}


def test_sqlite_state_store_write_and_read_available(tmp_path):
    store = llmbroker.sqlite.StateStore(str(tmp_path / "b.db"))
    state = LLMState(phase=LifecyclePhase.AVAILABLE, fail_count=3)

    async def run():
        await store.write("p1", state)
        result = await store.read()
        assert "p1" in result
        assert result["p1"].phase is LifecyclePhase.AVAILABLE
        assert result["p1"].fail_count == 3
        assert result["p1"].cooldown_until is None

    asyncio.run(run())


def test_sqlite_state_store_write_and_read_cooling(tmp_path):
    store = llmbroker.sqlite.StateStore(str(tmp_path / "b.db"))
    future = datetime.now(UTC) + timedelta(seconds=120)
    state = LLMState(phase=LifecyclePhase.COOLING, cooldown_until=future, fail_count=2)

    async def run():
        await store.write("p1", state)
        result = await store.read()
        assert result["p1"].phase is LifecyclePhase.COOLING
        assert result["p1"].cooldown_until is not None
        assert result["p1"].fail_count == 2

    asyncio.run(run())


def test_sqlite_state_store_expired_cooling_reads_as_available(tmp_path):
    """A stored COOLING state whose cooldown_until is in the past is re-derived as AVAILABLE."""
    store = llmbroker.sqlite.StateStore(str(tmp_path / "b.db"))
    past = datetime.now(UTC) - timedelta(seconds=1)
    state = LLMState(phase=LifecyclePhase.COOLING, cooldown_until=past, fail_count=1)

    async def run():
        await store.write("p1", state)
        result = await store.read()
        assert result["p1"].phase is LifecyclePhase.AVAILABLE
        assert result["p1"].cooldown_until is None
        assert result["p1"].fail_count == 1

    asyncio.run(run())


def test_sqlite_state_store_overwrite(tmp_path):
    """Writing the same LLM name twice replaces the previous entry."""
    store = llmbroker.sqlite.StateStore(str(tmp_path / "b.db"))
    future = datetime.now(UTC) + timedelta(seconds=60)

    async def run():
        await store.write(
            "p1", LLMState(phase=LifecyclePhase.COOLING, cooldown_until=future, fail_count=1)
        )
        await store.write("p1", LLMState(phase=LifecyclePhase.AVAILABLE, fail_count=0))
        result = await store.read()
        assert result["p1"].phase is LifecyclePhase.AVAILABLE

    asyncio.run(run())


def test_sqlite_state_store_per_user_isolated(tmp_path):
    """State written for one user is not visible to another user."""
    store = llmbroker.sqlite.StateStore(str(tmp_path / "b.db"))
    future = datetime.now(UTC) + timedelta(seconds=60)

    async def run():
        await store.write(
            "p1",
            LLMState(phase=LifecyclePhase.COOLING, cooldown_until=future, fail_count=1),
            "alice",
        )
        alice = await store.read("alice")
        bob = await store.read("bob")
        unscoped = await store.read()
        assert "p1" in alice
        assert "p1" not in bob
        assert "p1" not in unscoped

    asyncio.run(run())


def test_sqlite_state_store_user_id_none_unscoped(tmp_path):
    """user_id=None scopes to unscoped (NULL) rows only."""
    store = llmbroker.sqlite.StateStore(str(tmp_path / "b.db"))

    async def run():
        await store.write("shared", LLMState(fail_count=5))
        await store.write("alice-p1", LLMState(fail_count=1), "alice")
        result = await store.read()
        assert list(result) == ["shared"]
        assert result["shared"].fail_count == 5

    asyncio.run(run())

"""Tests for the broker's private in-memory live state and the SQLite state store."""

import asyncio
from datetime import UTC, datetime, timedelta

import llmbroker.sqlite

from llmbroker.models import LifecyclePhase, LLMState
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

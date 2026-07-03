"""Unit tests for LLMPool: slot invariants and key-resolution handling."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llmbroker.broker.pool import (
    TIER_DEMOTED_EVIDENCED,
    TIER_DEMOTED_UNTRIED,
    TIER_DEPRECATED,
    TIER_NORMAL,
    LLMPool,
    _STORE_CACHE_TTL,
)
from llmbroker.models import LifecyclePhase, LLMConfig, LLMState
from llmbroker.optimizer import FirstAvailablePolicy
from llmbroker.sqlite.state_store import StateStore


def _cfg(name="p1") -> LLMConfig:
    return LLMConfig(name=name, base_url="https://x/v1", model="m", api_key_ref="K")


def test_add_new_enqueues_one_slot():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), "key")
    assert pool._queue.qsize() == 1
    assert "p1" in pool


def test_add_existing_does_not_enqueue_extra_slot():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), "key")
    pool.add(_cfg(), "key2")  # same name — update, no extra queue slot
    assert pool._queue.qsize() == 1


def test_add_none_key_preserves_existing_key():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), "original")
    pool.add(_cfg(), None)  # None means "leave key intact"
    assert pool.resolved_key("p1") == "original"


def test_add_nonnone_key_overwrites_existing_key():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), "old")
    pool.add(_cfg(), "new")
    assert pool.resolved_key("p1") == "new"


def test_add_with_none_key_for_new_entry_leaves_no_key():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), None)
    assert not pool.has_key("p1")


def test_add_keyless_does_not_enqueue():
    """A config added without a resolved key stays visible but is never routable."""
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), None)
    assert "p1" in pool
    assert pool._queue.qsize() == 0


def test_add_keyless_then_keyed_enqueues_exactly_one_slot():
    """The keyless→keyed transition of an existing entry enqueues its slot exactly once.

    Distinct from test_add_existing_does_not_enqueue_extra_slot (which re-adds an
    already-keyed config) — here the config starts keyless and only later gets a key.
    """
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), None)
    assert pool._queue.qsize() == 0
    pool.add(_cfg(), "key")
    assert pool._queue.qsize() == 1
    pool.add(_cfg(), "key2")  # already keyed — no second slot
    assert pool._queue.qsize() == 1


def test_drop_removes_config_and_key():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), "key")
    pool.drop("p1")
    assert "p1" not in pool
    assert not pool.has_key("p1")


def test_drop_nonexistent_does_not_raise():
    pool = LLMPool(state_store=None, user_id=None)
    pool.drop("ghost")  # must be silent


def test_drop_clears_benched_so_a_readded_config_is_routable():
    """Regression: drop() used to leave stale tier state behind, so a fresh config
    re-added under the same name silently inherited the old benched latch forever."""
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), "key")
    pool.set_benched("p1")
    pool.drop("p1")

    pool.add(_cfg(), "key")
    assert not pool.is_benched("p1")
    assert pool._queue.qsize() == 1


def test_drop_clears_deprecated_demoted_and_alert_state():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), "key")
    pool.set_deprecated("p1")
    pool.update_demotions("p1", frozenset({"summarize"}), globally_demoted=True)
    pool._last_degraded_alert[("p1", "summarize", TIER_DEMOTED_EVIDENCED)] = 123.0

    pool.drop("p1")

    assert not pool.is_deprecated("p1")
    assert pool.demoted_operations("p1") == frozenset()
    assert not pool.is_globally_demoted("p1")
    assert not any(key[0] == "p1" for key in pool._last_degraded_alert)


def test_len_tracks_membership():
    pool = LLMPool(state_store=None, user_id=None)
    assert len(pool) == 0
    pool.add(_cfg("a"), "k")
    pool.add(_cfg("b"), "k")
    assert len(pool) == 2
    pool.drop("a")
    assert len(pool) == 1


# --- shared-state cache ---


def _make_store(states: dict[str, LLMState] | None = None, *, raise_on_read: bool = False):
    store = MagicMock()
    if raise_on_read:
        store.read = AsyncMock(side_effect=Exception("boom"))
    else:
        store.read = AsyncMock(return_value=states or {})
    store.write = AsyncMock()
    return store


def _cooling_state(seconds_ahead: float, fail_count: int = 1) -> LLMState:
    return LLMState(
        phase=LifecyclePhase.COOLING,
        cooldown_until=datetime.now(UTC) + timedelta(seconds=seconds_ahead),
        fail_count=fail_count,
    )


def test_apply_shared_cooling_no_store():
    async def run():
        pool = LLMPool(state_store=None, user_id=None)
        pool.add(_cfg(), "k")
        result = await pool.apply_shared_cooling(_cfg())
        assert result is False

    asyncio.run(run())


def test_apply_shared_cooling_store_error():
    async def run():
        store = _make_store(raise_on_read=True)
        pool = LLMPool(state_store=store, user_id=None)
        pool.add(_cfg(), "k")
        result = await pool.apply_shared_cooling(_cfg())
        assert result is False

    asyncio.run(run())


def test_apply_shared_cooling_not_in_shared():
    async def run():
        store = _make_store({})
        pool = LLMPool(state_store=store, user_id=None)
        pool.add(_cfg(), "k")
        result = await pool.apply_shared_cooling(_cfg())
        assert result is False

    asyncio.run(run())


def test_apply_shared_cooling_available():
    async def run():
        store = _make_store({"p1": LLMState(phase=LifecyclePhase.AVAILABLE)})
        pool = LLMPool(state_store=store, user_id=None)
        pool.add(_cfg(), "k")
        result = await pool.apply_shared_cooling(_cfg())
        assert result is False

    asyncio.run(run())


def test_apply_shared_cooling_cooling_no_until():
    async def run():
        store = _make_store({"p1": LLMState(phase=LifecyclePhase.COOLING, cooldown_until=None)})
        pool = LLMPool(state_store=store, user_id=None)
        pool.add(_cfg(), "k")
        result = await pool.apply_shared_cooling(_cfg())
        assert result is False

    asyncio.run(run())


def test_apply_shared_cooling_expired():
    async def run():
        store = _make_store({"p1": _cooling_state(-5)})
        pool = LLMPool(state_store=store, user_id=None)
        pool.add(_cfg(), "k")
        result = await pool.apply_shared_cooling(_cfg())
        assert result is False

    asyncio.run(run())


def test_apply_shared_cooling_active():
    async def run():
        cfg = _cfg()
        stored = _cooling_state(30)
        store = _make_store({"p1": stored})
        pool = LLMPool(state_store=store, user_id=None)
        pool.add(cfg, "k")
        cfg = await pool.acquire(0)  # mirror router: dequeue before apply

        mock_loop = MagicMock()
        with patch("llmbroker.broker.pool.asyncio.get_running_loop", return_value=mock_loop):
            result = await pool.apply_shared_cooling(cfg)

        assert result is True
        assert pool.state("p1").phase is LifecyclePhase.COOLING
        assert pool.state("p1").cooldown_until == stored.cooldown_until
        assert mock_loop.call_later.called
        call_args = mock_loop.call_later.call_args
        assert abs(call_args[0][0] - 30) < 1.0
        # callback is _reenqueue_config; calling it with the config arg should re-add the slot
        callback, cfg_arg = call_args[0][1], call_args[0][2]
        size_before = pool._queue.qsize()
        callback(cfg_arg)
        assert pool._queue.qsize() == size_before + 1

    asyncio.run(run())


def test_apply_shared_cooling_preserves_local_fail_count():
    async def run():
        cfg = _cfg()
        store = _make_store({"p1": _cooling_state(30, fail_count=2)})
        pool = LLMPool(state_store=store, user_id=None)
        pool.add(cfg, "k")
        cfg = await pool.acquire(0)  # mirror router: dequeue before apply
        # Inject local fail_count=5 via set_cooling
        pool._state.set_cooling("p1", datetime.now(UTC) + timedelta(seconds=60), 5)

        mock_loop = MagicMock()
        with patch("llmbroker.broker.pool.asyncio.get_running_loop", return_value=mock_loop):
            await pool.apply_shared_cooling(cfg)

        assert pool.state("p1").fail_count == 5

    asyncio.run(run())


def test_store_cache_hit():
    async def run():
        store = _make_store({})
        pool = LLMPool(state_store=store, user_id=None)
        await pool._get_store_cache()
        await pool._get_store_cache()
        store.read.assert_called_once()

    asyncio.run(run())


def test_store_cache_expires():
    async def run():
        store = _make_store({})
        pool = LLMPool(state_store=store, user_id=None)
        # _get_store_cache reads time.monotonic() once per call and reuses it
        # for both the hit-check and the expiry stamp.
        # Values: [call1 (miss), call2 (expired → miss)]
        mono_values = [0.0, _STORE_CACHE_TTL + 0.1]
        with patch("llmbroker.broker.pool.time.monotonic", side_effect=mono_values):
            await pool._get_store_cache()
            await pool._get_store_cache()

        assert store.read.call_count == 2

    asyncio.run(run())


def test_cool_down_invalidates_cache():
    async def run():
        store = _make_store({})
        pool = LLMPool(state_store=store, user_id=None)
        cfg = _cfg()
        pool.add(cfg, "k")

        # Warm the cache
        await pool._get_store_cache()
        assert pool._store_cache is not None

        mock_loop = MagicMock()
        with patch("llmbroker.broker.pool.asyncio.get_running_loop", return_value=mock_loop):
            await pool.cool_down(cfg, 10)

        assert pool._store_cache is None

    asyncio.run(run())


def test_cross_process_cooldown_shared_via_store(tmp_path):
    async def run():
        db = tmp_path / "state.db"
        store_a = StateStore(db)
        store_b = StateStore(db)

        cfg_a = _cfg("x")
        cfg_b = _cfg("x")

        pool_a = LLMPool(state_store=store_a, user_id=None)
        pool_b = LLMPool(state_store=store_b, user_id=None)
        pool_a.add(cfg_a, "k")
        pool_b.add(cfg_b, "k")

        cfg_a = await pool_a.acquire(0)  # mirror router: dequeue before cool_down
        mock_loop = MagicMock()
        with patch("llmbroker.broker.pool.asyncio.get_running_loop", return_value=mock_loop):
            await pool_a.cool_down(cfg_a, 30)

        # Dequeue so apply_shared_cooling doesn't add a duplicate slot
        acquired = await pool_b.acquire(0)

        result = await pool_b.apply_shared_cooling(acquired)
        assert result is True
        assert pool_b.state("x").phase is LifecyclePhase.COOLING

    asyncio.run(run())


# --- SQLite cache integration ---


def test_sqlite_cache_hit_reduces_reads(tmp_path):
    """Within TTL, _get_store_cache reads SQLite only once regardless of call count."""

    async def run():
        store = StateStore(tmp_path / "state.db")
        pool = LLMPool(state_store=store, user_id=None)
        pool.add(_cfg(), "k")

        read_spy = AsyncMock(wraps=store.read)
        with patch.object(store, "read", read_spy):
            await pool._get_store_cache()  # miss → reads SQLite
            await pool._get_store_cache()  # hit → served from cache

        assert read_spy.call_count == 1

    asyncio.run(run())


def test_sqlite_cache_invalidated_after_cool_down(tmp_path):
    """After cool_down writes to SQLite and clears the cache, _get_store_cache returns fresh state."""

    async def run():
        store = StateStore(tmp_path / "state.db")
        pool = LLMPool(state_store=store, user_id=None)
        cfg = _cfg()
        pool.add(cfg, "k")

        first = await pool._get_store_cache()
        assert "p1" not in first

        mock_loop = MagicMock()
        with patch("llmbroker.broker.pool.asyncio.get_running_loop", return_value=mock_loop):
            await pool.cool_down(cfg, 30)
        assert pool._store_cache is None  # invalidated by cool_down

        fresh = await pool._get_store_cache()
        assert fresh["p1"].phase is LifecyclePhase.COOLING

    asyncio.run(run())


def test_sqlite_cache_stale_then_refreshes_after_ttl(tmp_path):
    """External write is invisible within TTL; after TTL expires the fresh state becomes visible."""

    async def run():
        db = tmp_path / "state.db"
        store_a = StateStore(db)
        store_b = StateStore(db)

        pool_a = LLMPool(state_store=store_a, user_id=None)
        pool_b = LLMPool(state_store=store_b, user_id=None)
        cfg_b = _cfg("x")
        pool_a.add(_cfg("x"), "k")
        pool_b.add(cfg_b, "k")

        # Warm pool_a's cache: SQLite is empty at this point.
        first = await pool_a._get_store_cache()
        assert "x" not in first

        # Pool B cools x, writing COOLING to SQLite. pool_a cache is not invalidated.
        mock_loop = MagicMock()
        with patch("llmbroker.broker.pool.asyncio.get_running_loop", return_value=mock_loop):
            await pool_b.cool_down(cfg_b, 30)

        # Within TTL: pool_a serves the stale (empty) cache, not yet seeing COOLING.
        within_ttl = await pool_a._get_store_cache()
        assert "x" not in within_ttl

        # Simulate TTL expiry by backdating the expiry timestamp.
        pool_a._store_cache_expires = 0.0

        # Past TTL: pool_a re-reads SQLite and now sees COOLING written by pool_b.
        past_ttl = await pool_a._get_store_cache()
        assert "x" in past_ttl
        assert past_ttl["x"].phase is LifecyclePhase.COOLING

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Manual bench (hard exclusion)
# ---------------------------------------------------------------------------


async def test_set_benched_drains_currently_queued_slot():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), "k")
    assert pool._queue.qsize() == 1
    pool.set_benched("p1")
    assert pool._queue.qsize() == 0
    assert pool.is_benched("p1")


async def test_benched_config_in_configs_but_never_acquired_even_as_only_candidate():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), "k")
    pool.set_benched("p1")
    assert "p1" in pool.configs
    with pytest.raises(asyncio.QueueEmpty):
        await pool.acquire(0, policy=FirstAvailablePolicy(), operation=None)


async def test_clear_benched_readmits_slot():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), "k")
    pool.set_benched("p1")
    assert pool._queue.qsize() == 0
    pool.clear_benched("p1")
    assert pool._queue.qsize() == 1


async def test_clear_benched_without_key_does_not_enqueue():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), None)  # keyless, never queued in the first place
    pool.set_benched("p1")
    pool.clear_benched("p1")
    assert pool._queue.qsize() == 0


async def test_add_while_benched_does_not_enqueue():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), None)
    pool.set_benched("p1")
    pool.add(_cfg(), "k")  # keyless -> keyed transition, but benched
    assert pool._queue.qsize() == 0


async def test_benched_mid_cooldown_not_reenqueued_on_expiry():
    """A model can be mid-cooldown when it gets manually benched — the cooldown
    callback must not resurrect it."""
    pool = LLMPool(state_store=None, user_id=None)
    cfg = _cfg()
    pool.add(cfg, "k")
    acquired = await pool.acquire(0)

    mock_loop = MagicMock()
    with patch("llmbroker.broker.pool.asyncio.get_running_loop", return_value=mock_loop):
        await pool.cool_down(acquired, 30)
    pool.set_benched("p1")

    callback, cfg_arg = mock_loop.call_later.call_args[0][1], mock_loop.call_later.call_args[0][2]
    callback(cfg_arg)
    assert pool._queue.qsize() == 0


async def test_release_of_benched_config_does_not_reenqueue():
    pool = LLMPool(state_store=None, user_id=None)
    cfg = _cfg()
    pool.add(cfg, "k")
    acquired = await pool.acquire(0)
    pool.set_benched("p1")
    pool.release(acquired)
    assert pool._queue.qsize() == 0


# ---------------------------------------------------------------------------
# Tiered selection: deprecated / demoted (soft — never withdraw the slot)
# ---------------------------------------------------------------------------


async def test_tier_of_defaults_to_normal():
    pool = LLMPool(state_store=None, user_id=None)
    assert pool.tier_of("x", None) == TIER_NORMAL


async def test_tier0_preferred_over_deprecated_when_both_available():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg("normal"), "k")
    pool.add(_cfg("dep"), "k")
    pool.set_deprecated("dep")
    picked = await pool.acquire(0, policy=FirstAvailablePolicy(), operation=None)
    assert picked.name == "normal"
    # the deprecated candidate is reinserted, not discarded
    assert pool._queue.qsize() == 1


async def test_deprecated_config_acquired_only_when_no_tier0_available():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg("dep"), "k")
    pool.set_deprecated("dep")
    picked = await pool.acquire(0, policy=FirstAvailablePolicy(), operation=None)
    assert picked.name == "dep"


async def test_clear_deprecated_restores_tier0():
    pool = LLMPool(state_store=None, user_id=None)
    pool.set_deprecated("x")
    assert pool.is_deprecated("x")
    pool.clear_deprecated("x")
    assert not pool.is_deprecated("x")


async def test_op_demoted_skipped_for_that_operation_while_alternative_exists():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg("good"), "k")
    pool.add(_cfg("bad"), "k")
    pool.update_demotions("bad", frozenset({"summarize"}), globally_demoted=False)
    picked = await pool.acquire(0, policy=FirstAvailablePolicy(), operation="summarize")
    assert picked.name == "good"


async def test_op_demoted_chosen_when_no_alternative():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg("bad"), "k")
    pool.update_demotions("bad", frozenset({"summarize"}), globally_demoted=False)
    picked = await pool.acquire(0, policy=FirstAvailablePolicy(), operation="summarize")
    assert picked.name == "bad"


async def test_op_demoted_acquired_normally_for_other_operations():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg("bad"), "k")
    pool.update_demotions("bad", frozenset({"summarize"}), globally_demoted=False)
    picked = await pool.acquire(0, policy=FirstAvailablePolicy(), operation="translate")
    assert picked.name == "bad"


async def test_globally_demoted_untried_operation_ranks_ahead_of_evidenced_bad():
    pool = LLMPool(state_store=None, user_id=None)
    pool.update_demotions("x", frozenset({"summarize"}), globally_demoted=True)
    assert pool.tier_of("x", "translate") == TIER_DEMOTED_UNTRIED  # untried -> new territory
    assert pool.tier_of("x", "summarize") == TIER_DEMOTED_EVIDENCED  # evidenced bad
    assert TIER_DEMOTED_UNTRIED < TIER_DEMOTED_EVIDENCED


async def test_update_demotions_clearing_globally_demoted_restores_untried_tier():
    pool = LLMPool(state_store=None, user_id=None)
    pool.update_demotions("x", frozenset({"summarize"}), globally_demoted=True)
    pool.update_demotions("x", frozenset(), globally_demoted=False)
    assert pool.tier_of("x", "translate") == TIER_NORMAL
    assert pool.tier_of("x", "summarize") == TIER_NORMAL
    assert not pool.is_globally_demoted("x")


async def test_every_model_demoted_pool_still_serves():
    """A rater that scores everything low demotes everything to tier 2/3 — the pool
    keeps operating on transport ranking within that tier, never goes empty."""
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg("a"), "k")
    pool.add(_cfg("b"), "k")
    pool.update_demotions("a", frozenset({None}), globally_demoted=True)
    pool.update_demotions("b", frozenset({None}), globally_demoted=True)
    picked = await pool.acquire(0, policy=FirstAvailablePolicy(), operation=None)
    assert picked.name in ("a", "b")


# ---------------------------------------------------------------------------
# Degraded-tier alert (debounced)
# ---------------------------------------------------------------------------


async def test_degraded_tier_alert_fires_debounced():
    alerts = []
    pool = LLMPool(
        state_store=None,
        user_id=None,
        on_degraded_tier=lambda name, op, tier: alerts.append((name, op, tier)),
    )
    pool.add(_cfg("dep"), "k")
    pool.set_deprecated("dep")

    picked = await pool.acquire(0, policy=FirstAvailablePolicy(), operation=None)
    pool.release(picked)
    picked2 = await pool.acquire(0, policy=FirstAvailablePolicy(), operation=None)
    pool.release(picked2)

    assert len(alerts) == 1
    assert alerts[0] == ("dep", None, TIER_DEPRECATED)


async def test_no_degraded_tier_alert_for_tier0():
    alerts = []
    pool = LLMPool(
        state_store=None,
        user_id=None,
        on_degraded_tier=lambda name, op, tier: alerts.append((name, op, tier)),
    )
    pool.add(_cfg("normal"), "k")
    await pool.acquire(0, policy=FirstAvailablePolicy(), operation=None)
    assert alerts == []


async def test_degraded_tier_alert_fires_again_on_escalation_within_debounce_window():
    """Regression: the alert dedup key didn't include tier, so an escalation from a
    milder tier to a worse one within the realert window was silently suppressed."""
    alerts = []
    pool = LLMPool(
        state_store=None,
        user_id=None,
        on_degraded_tier=lambda name, op, tier: alerts.append((name, op, tier)),
    )
    pool.add(_cfg("dep"), "k")
    pool.set_deprecated("dep")

    picked = await pool.acquire(0, policy=FirstAvailablePolicy(), operation=None)
    pool.release(picked)
    assert alerts == [("dep", None, TIER_DEPRECATED)]

    # Escalate to quality-demoted (a worse tier), well within the 60s realert window.
    pool.update_demotions("dep", frozenset({None}), globally_demoted=False)
    picked2 = await pool.acquire(0, policy=FirstAvailablePolicy(), operation=None)
    pool.release(picked2)

    assert alerts == [
        ("dep", None, TIER_DEPRECATED),
        ("dep", None, TIER_DEMOTED_EVIDENCED),
    ]


async def test_no_degraded_tier_alert_when_callback_not_configured():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg("dep"), "k")
    pool.set_deprecated("dep")
    picked = await pool.acquire(0, policy=FirstAvailablePolicy(), operation=None)
    assert picked.name == "dep"  # no callback configured — must not raise

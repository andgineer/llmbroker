"""Unit tests for LLMPool: slot invariants, key-resolution handling, and selection."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llmbroker.broker.pool import _STORE_CACHE_TTL, LLMPool
from llmbroker.models import LifecyclePhase, LLMConfig, LLMState
from llmbroker.optimizer import Optimizer
from llmbroker.sqlite.state_store import StateStore


def _cfg(name="p1", *, parallel: int | None = None) -> LLMConfig:
    return LLMConfig(
        name=name, base_url="https://x/v1", model="m", api_key_ref="K", parallel=parallel
    )


async def test_add_new_registers_one_slot():
    pool = LLMPool(state_store=None, user_id=None)
    await pool.add(_cfg(), "key")
    assert len(pool) == 1
    assert "p1" in pool


async def test_add_existing_does_not_add_extra_slot():
    pool = LLMPool(state_store=None, user_id=None)
    await pool.add(_cfg(), "key")
    await pool.add(_cfg(), "key2")  # same name — update, no extra slot
    assert len(pool) == 1


async def test_add_none_key_preserves_existing_key():
    pool = LLMPool(state_store=None, user_id=None)
    await pool.add(_cfg(), "original")
    await pool.add(_cfg(), None)  # None means "leave key intact"
    assert pool.resolved_key("p1") == "original"


async def test_add_nonnone_key_overwrites_existing_key():
    pool = LLMPool(state_store=None, user_id=None)
    await pool.add(_cfg(), "old")
    await pool.add(_cfg(), "new")
    assert pool.resolved_key("p1") == "new"


async def test_add_with_none_key_for_new_entry_leaves_no_key():
    pool = LLMPool(state_store=None, user_id=None)
    await pool.add(_cfg(), None)
    assert not pool.has_key("p1")


async def test_add_keyless_is_never_available():
    """A config added without a resolved key stays visible but is never routable."""
    pool = LLMPool(state_store=None, user_id=None)
    await pool.add(_cfg(), None)
    assert "p1" in pool
    with pytest.raises(TimeoutError):
        await pool.acquire(0)


async def test_add_keyless_then_keyed_becomes_acquirable():
    """The keyless→keyed transition of an existing entry makes the slot acquirable."""
    pool = LLMPool(state_store=None, user_id=None)
    await pool.add(_cfg(), None)
    with pytest.raises(TimeoutError):
        await pool.acquire(0)
    await pool.add(_cfg(), "key")
    picked = await pool.acquire(0)
    assert picked.name == "p1"


async def test_drop_removes_config_and_key():
    pool = LLMPool(state_store=None, user_id=None)
    await pool.add(_cfg(), "key")
    await pool.drop("p1")
    assert "p1" not in pool
    assert not pool.has_key("p1")


async def test_drop_nonexistent_does_not_raise():
    pool = LLMPool(state_store=None, user_id=None)
    await pool.drop("ghost")  # must be silent


async def test_drop_clears_disabled_so_a_readded_config_is_routable():
    """Regression: drop() used to leave stale state behind, so a fresh config
    re-added under the same name silently inherited the old disabled latch forever."""
    pool = LLMPool(state_store=None, user_id=None)
    await pool.add(_cfg(), "key")
    pool.set_disabled("p1")
    await pool.drop("p1")

    await pool.add(_cfg(), "key")
    assert not pool.is_disabled("p1")
    picked = await pool.acquire(0)
    assert picked.name == "p1"


async def test_len_tracks_membership():
    pool = LLMPool(state_store=None, user_id=None)
    assert len(pool) == 0
    await pool.add(_cfg("a"), "k")
    await pool.add(_cfg("b"), "k")
    assert len(pool) == 2
    await pool.drop("a")
    assert len(pool) == 1


# --- phase / fail-count derivation (pool.state) ---


def test_fresh_llm_is_available():
    pool = LLMPool(state_store=None, user_id=None)
    s = pool.state("p1")
    assert s.phase is LifecyclePhase.AVAILABLE
    assert s.cooldown_until is None
    assert s.fail_count == 0


async def test_cooling_until_future_reports_cooling():
    pool = LLMPool(state_store=None, user_id=None)
    cfg = _cfg()
    await pool.add(cfg, "k")
    await pool.cool_down(cfg, 60)
    s = pool.state("p1")
    assert s.phase is LifecyclePhase.COOLING
    assert s.cooldown_until is not None
    assert s.fail_count == 1


async def test_cooling_in_past_reports_available():
    pool = LLMPool(state_store=None, user_id=None)
    cfg = _cfg()
    await pool.add(cfg, "k")
    slot = pool._slots["p1"]
    slot.cooldown_until = datetime.now(UTC) - timedelta(seconds=1)
    slot.fail_count = 2
    s = pool.state("p1")
    assert s.phase is LifecyclePhase.AVAILABLE
    assert s.cooldown_until is None
    # fail_count is retained even after cooldown clears
    assert s.fail_count == 2


async def test_clear_cooling_resets_to_available():
    pool = LLMPool(state_store=None, user_id=None)
    cfg = _cfg()
    await pool.add(cfg, "k")
    await pool.cool_down(cfg, 60)
    pool.clear_cooling("p1")
    assert pool.state("p1").phase is LifecyclePhase.AVAILABLE


async def test_mark_quality_fail_increments():
    pool = LLMPool(state_store=None, user_id=None)
    await pool.add(_cfg(), "k")
    pool.mark_quality_fail("p1")
    pool.mark_quality_fail("p1")
    assert pool.state("p1").fail_count == 2


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


async def test_apply_shared_cooling_no_store():
    pool = LLMPool(state_store=None, user_id=None)
    await pool.add(_cfg(), "k")
    result = await pool.apply_shared_cooling(_cfg())
    assert result is False


async def test_apply_shared_cooling_store_error():
    store = _make_store(raise_on_read=True)
    pool = LLMPool(state_store=store, user_id=None)
    await pool.add(_cfg(), "k")
    result = await pool.apply_shared_cooling(_cfg())
    assert result is False


async def test_apply_shared_cooling_not_in_shared():
    store = _make_store({})
    pool = LLMPool(state_store=store, user_id=None)
    await pool.add(_cfg(), "k")
    result = await pool.apply_shared_cooling(_cfg())
    assert result is False


async def test_apply_shared_cooling_available():
    store = _make_store({"p1": LLMState(phase=LifecyclePhase.AVAILABLE)})
    pool = LLMPool(state_store=store, user_id=None)
    await pool.add(_cfg(), "k")
    result = await pool.apply_shared_cooling(_cfg())
    assert result is False


async def test_apply_shared_cooling_cooling_no_until():
    store = _make_store({"p1": LLMState(phase=LifecyclePhase.COOLING, cooldown_until=None)})
    pool = LLMPool(state_store=store, user_id=None)
    await pool.add(_cfg(), "k")
    result = await pool.apply_shared_cooling(_cfg())
    assert result is False


async def test_apply_shared_cooling_expired():
    store = _make_store({"p1": _cooling_state(-5)})
    pool = LLMPool(state_store=store, user_id=None)
    await pool.add(_cfg(), "k")
    result = await pool.apply_shared_cooling(_cfg())
    assert result is False


async def test_apply_shared_cooling_unknown_slot():
    store = _make_store({"p1": _cooling_state(30)})
    pool = LLMPool(state_store=store, user_id=None)
    result = await pool.apply_shared_cooling(_cfg())
    assert result is False


async def test_apply_shared_cooling_active():
    cfg = _cfg()
    stored = _cooling_state(30)
    store = _make_store({"p1": stored})
    pool = LLMPool(state_store=store, user_id=None)
    await pool.add(cfg, "k")
    cfg = await pool.acquire(0)  # mirror router: acquire before apply

    result = await pool.apply_shared_cooling(cfg)

    assert result is True
    assert pool.state("p1").phase is LifecyclePhase.COOLING
    assert pool.state("p1").cooldown_until == stored.cooldown_until
    assert pool._slots["p1"].in_flight == 0


async def test_apply_shared_cooling_preserves_local_fail_count():
    cfg = _cfg()
    store = _make_store({"p1": _cooling_state(30, fail_count=2)})
    pool = LLMPool(state_store=store, user_id=None)
    await pool.add(cfg, "k")
    cfg = await pool.acquire(0)  # mirror router: acquire before apply
    pool._slots["p1"].fail_count = 5

    await pool.apply_shared_cooling(cfg)

    assert pool.state("p1").fail_count == 5


async def test_store_cache_hit():
    store = _make_store({})
    pool = LLMPool(state_store=store, user_id=None)
    await pool._get_store_cache()
    await pool._get_store_cache()
    store.read.assert_called_once()


async def test_store_cache_expires():
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


async def test_cool_down_invalidates_cache():
    store = _make_store({})
    pool = LLMPool(state_store=store, user_id=None)
    cfg = _cfg()
    await pool.add(cfg, "k")

    # Warm the cache
    await pool._get_store_cache()
    assert pool._store_cache is not None

    await pool.cool_down(cfg, 10)

    assert pool._store_cache is None


async def test_cool_down_unknown_slot_is_noop():
    store = _make_store({})
    pool = LLMPool(state_store=store, user_id=None)
    cfg = _cfg()
    await pool.cool_down(cfg, 10)  # never added — must not raise
    store.write.assert_not_called()


async def test_cross_process_cooldown_shared_via_store(tmp_path):
    db = tmp_path / "state.db"
    store_a = StateStore(db)
    store_b = StateStore(db)

    cfg_a = _cfg("x")
    cfg_b = _cfg("x")

    pool_a = LLMPool(state_store=store_a, user_id=None)
    pool_b = LLMPool(state_store=store_b, user_id=None)
    await pool_a.add(cfg_a, "k")
    await pool_b.add(cfg_b, "k")

    cfg_a = await pool_a.acquire(0)  # mirror router: acquire before cool_down
    await pool_a.cool_down(cfg_a, 30)

    # Acquire so apply_shared_cooling doesn't fight over the same slot
    acquired = await pool_b.acquire(0)

    result = await pool_b.apply_shared_cooling(acquired)
    assert result is True
    assert pool_b.state("x").phase is LifecyclePhase.COOLING


# --- SQLite cache integration ---


async def test_sqlite_cache_hit_reduces_reads(tmp_path):
    """Within TTL, _get_store_cache reads SQLite only once regardless of call count."""
    store = StateStore(tmp_path / "state.db")
    pool = LLMPool(state_store=store, user_id=None)
    await pool.add(_cfg(), "k")

    read_spy = AsyncMock(wraps=store.read)
    with patch.object(store, "read", read_spy):
        await pool._get_store_cache()  # miss → reads SQLite
        await pool._get_store_cache()  # hit → served from cache

    assert read_spy.call_count == 1


async def test_sqlite_cache_invalidated_after_cool_down(tmp_path):
    """After cool_down writes to SQLite and clears the cache, _get_store_cache returns fresh state."""
    store = StateStore(tmp_path / "state.db")
    pool = LLMPool(state_store=store, user_id=None)
    cfg = _cfg()
    await pool.add(cfg, "k")

    first = await pool._get_store_cache()
    assert "p1" not in first

    await pool.cool_down(cfg, 30)
    assert pool._store_cache is None  # invalidated by cool_down

    fresh = await pool._get_store_cache()
    assert fresh["p1"].phase is LifecyclePhase.COOLING


async def test_sqlite_cache_stale_then_refreshes_after_ttl(tmp_path):
    """External write is invisible within TTL; after TTL expires the fresh state becomes visible."""
    db = tmp_path / "state.db"
    store_a = StateStore(db)
    store_b = StateStore(db)

    pool_a = LLMPool(state_store=store_a, user_id=None)
    pool_b = LLMPool(state_store=store_b, user_id=None)
    cfg_b = _cfg("x")
    await pool_a.add(_cfg("x"), "k")
    await pool_b.add(cfg_b, "k")

    # Warm pool_a's cache: SQLite is empty at this point.
    first = await pool_a._get_store_cache()
    assert "x" not in first

    # Pool B cools x, writing COOLING to SQLite. pool_a cache is not invalidated.
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


# ---------------------------------------------------------------------------
# Manual disable (hard exclusion)
# ---------------------------------------------------------------------------


async def test_set_disabled_excludes_slot_immediately():
    pool = LLMPool(state_store=None, user_id=None)
    await pool.add(_cfg(), "k")
    pool.set_disabled("p1")
    assert pool.is_disabled("p1")
    with pytest.raises(TimeoutError):
        await pool.acquire(0)


async def test_disabled_config_in_configs_but_never_acquired_even_as_only_candidate():
    pool = LLMPool(state_store=None, user_id=None)
    await pool.add(_cfg(), "k")
    pool.set_disabled("p1")
    assert "p1" in pool.configs
    with pytest.raises(TimeoutError):
        await pool.acquire(0)


async def test_clear_disabled_readmits_slot():
    pool = LLMPool(state_store=None, user_id=None)
    await pool.add(_cfg(), "k")
    pool.set_disabled("p1")
    await pool.clear_disabled("p1")
    picked = await pool.acquire(0)
    assert picked.name == "p1"


async def test_clear_disabled_without_key_does_not_make_it_acquirable():
    pool = LLMPool(state_store=None, user_id=None)
    await pool.add(_cfg(), None)  # keyless, never routable in the first place
    pool.set_disabled("p1")
    await pool.clear_disabled("p1")
    with pytest.raises(TimeoutError):
        await pool.acquire(0)


async def test_add_while_disabled_does_not_make_it_acquirable():
    pool = LLMPool(state_store=None, user_id=None)
    await pool.add(_cfg(), None)
    pool.set_disabled("p1")
    await pool.add(_cfg(), "k")  # keyless -> keyed transition, but disabled
    with pytest.raises(TimeoutError):
        await pool.acquire(0)


async def test_disabled_mid_cooldown_stays_excluded_after_cooldown_expires():
    """A model can be mid-cooldown when it gets manually disabled — expiry must not
    resurrect it."""
    pool = LLMPool(state_store=None, user_id=None)
    cfg = _cfg()
    await pool.add(cfg, "k")
    acquired = await pool.acquire(0)

    await pool.cool_down(acquired, 30)
    pool.set_disabled("p1")
    pool._slots["p1"].cooldown_until = datetime.now(UTC) - timedelta(seconds=1)  # expire it

    with pytest.raises(TimeoutError):
        await pool.acquire(0)


async def test_release_of_disabled_config_leaves_it_excluded():
    pool = LLMPool(state_store=None, user_id=None)
    cfg = _cfg()
    await pool.add(cfg, "k")
    acquired = await pool.acquire(0)
    pool.set_disabled("p1")
    await pool.release(acquired)
    with pytest.raises(TimeoutError):
        await pool.acquire(0)


async def test_release_of_dropped_config_is_a_noop():
    pool = LLMPool(state_store=None, user_id=None)
    cfg = _cfg()
    await pool.add(cfg, "k")
    acquired = await pool.acquire(0)
    await pool.drop("p1")
    await pool.release(acquired)  # must not raise


# ---------------------------------------------------------------------------
# acquire(): curated order, demoted-last, waiting
# ---------------------------------------------------------------------------


async def test_acquire_wait_zero_raises_immediately_when_empty():
    pool = LLMPool(state_store=None, user_id=None)
    with pytest.raises(TimeoutError):
        await pool.acquire(0)


async def test_acquire_finite_wait_times_out_at_deadline():
    pool = LLMPool(state_store=None, user_id=None)
    await pool.add(_cfg(parallel=1), "k")
    await pool.acquire(0)  # occupy the only slot
    with pytest.raises(TimeoutError):
        await pool.acquire(0.05)


async def test_acquire_wait_none_wakes_when_cooldown_expires_without_a_timer():
    pool = LLMPool(state_store=None, user_id=None)
    cfg = _cfg()
    await pool.add(cfg, "k")
    acquired = await pool.acquire(0)
    await pool.cool_down(acquired, 0.05)

    picked = await asyncio.wait_for(pool.acquire(None), timeout=2.0)
    assert picked.name == "p1"


async def test_acquire_waiter_wakes_on_release():
    pool = LLMPool(state_store=None, user_id=None)
    cfg = _cfg(parallel=1)
    await pool.add(cfg, "k")
    acquired = await pool.acquire(0)

    waiter = asyncio.ensure_future(pool.acquire(None))
    await asyncio.sleep(0.01)
    assert not waiter.done()

    await pool.release(acquired)
    picked = await asyncio.wait_for(waiter, timeout=2.0)
    assert picked.name == "p1"


async def test_curated_order_preferred_best_available_takes_all_traffic():
    """Curated priority: the best (lowest-order) available slot is picked every time,
    not round-robin — round-robin was removed."""
    pool = LLMPool(state_store=None, user_id=None)
    await pool.add(_cfg("a"), "k", order=0)
    await pool.add(_cfg("b"), "k", order=1)
    await pool.add(_cfg("c"), "k", order=2)

    picked_names = []
    for _ in range(4):
        picked = await pool.acquire(0)
        picked_names.append(picked.name)
        await pool.release(picked)

    assert picked_names == ["a", "a", "a", "a"]


async def test_curated_order_falls_back_to_next_when_best_is_cooling():
    pool = LLMPool(state_store=None, user_id=None)
    cfg_a = _cfg("a")
    await pool.add(cfg_a, "k", order=0)
    await pool.add(_cfg("b"), "k", order=1)

    acquired_a = await pool.acquire(0)
    await pool.cool_down(acquired_a, 60)

    picked = await pool.acquire(0)
    assert picked.name == "b"


async def test_add_reasserts_curated_order_on_refresh():
    pool = LLMPool(state_store=None, user_id=None)
    await pool.add(_cfg("a"), "k", order=5)
    await pool.add(_cfg("b"), "k", order=1)
    picked = await pool.acquire(0)
    assert picked.name == "b"


async def test_pool_acquire_returns_only_available_slot():
    pool = LLMPool(state_store=None, user_id=None)
    a = _cfg("a")
    await pool.add(a, "k")
    result = await pool.acquire(0)
    assert result is a


# ---------------------------------------------------------------------------
# Demoted-last selection (Optimizer.is_demoted feeds the sort key)
# ---------------------------------------------------------------------------


async def test_demoted_for_operation_sorts_last_while_alternative_exists():
    opt = Optimizer(quality_min_count=10, quality_floor=0.3)
    for _ in range(10):
        opt.record_quality("bad", "summarize", 0.0)
    pool = LLMPool(state_store=None, user_id=None, optimizer=opt)
    await pool.add(_cfg("bad"), "k", order=0)
    await pool.add(_cfg("good"), "k", order=1)

    picked = await pool.acquire(0, operation="summarize")
    assert picked.name == "good"


async def test_demoted_model_still_serves_when_no_alternative():
    opt = Optimizer(quality_min_count=10, quality_floor=0.3)
    for _ in range(10):
        opt.record_quality("bad", "summarize", 0.0)
    pool = LLMPool(state_store=None, user_id=None, optimizer=opt)
    await pool.add(_cfg("bad"), "k")

    picked = await pool.acquire(0, operation="summarize")
    assert picked.name == "bad"


async def test_demotion_is_per_operation_not_global():
    opt = Optimizer(quality_min_count=10, quality_floor=0.3)
    for _ in range(10):
        opt.record_quality("bad", "summarize", 0.0)
    pool = LLMPool(state_store=None, user_id=None, optimizer=opt)
    await pool.add(_cfg("bad"), "k")

    picked = await pool.acquire(0, operation="translate")
    assert picked.name == "bad"


async def test_every_model_demoted_pool_still_serves():
    """A rater that scores everything low demotes everything — the pool keeps
    operating on curated order within the demoted set, never goes empty."""
    opt = Optimizer(quality_min_count=10, quality_floor=0.3)
    for _ in range(10):
        opt.record_quality("a", None, 0.0)
        opt.record_quality("b", None, 0.0)
    pool = LLMPool(state_store=None, user_id=None, optimizer=opt)
    await pool.add(_cfg("a"), "k")
    await pool.add(_cfg("b"), "k")

    picked = await pool.acquire(0, operation=None)
    assert picked.name in ("a", "b")


async def test_no_optimizer_never_demotes():
    pool = LLMPool(state_store=None, user_id=None)
    await pool.add(_cfg("a"), "k", order=0)
    picked = await pool.acquire(0, operation="summarize")
    assert picked.name == "a"


# ---------------------------------------------------------------------------
# Configurable per-LLM concurrency (parallel=None default is unlimited)
# ---------------------------------------------------------------------------


async def test_two_concurrent_acquires_of_one_llm_both_succeed_by_default():
    pool = LLMPool(state_store=None, user_id=None)
    cfg = _cfg()
    await pool.add(cfg, "k")

    first = await pool.acquire(0)
    second = await pool.acquire(0)
    assert first.name == "p1"
    assert second.name == "p1"
    assert pool._slots["p1"].in_flight == 2


async def test_parallel_one_serializes_like_today():
    """parallel=1 reproduces yesterday's one-in-flight-per-LLM behavior exactly."""
    pool = LLMPool(state_store=None, user_id=None)
    cfg = _cfg(parallel=1)
    await pool.add(cfg, "k")

    await pool.acquire(0)
    with pytest.raises(TimeoutError):
        await pool.acquire(0)


async def test_release_of_one_parallel_call_frees_exactly_one_slot():
    pool = LLMPool(state_store=None, user_id=None)
    cfg = _cfg(parallel=2)
    await pool.add(cfg, "k")

    first = await pool.acquire(0)
    await pool.acquire(0)
    with pytest.raises(TimeoutError):
        await pool.acquire(0)

    await pool.release(first)
    picked = await pool.acquire(0)
    assert picked.name == "p1"


async def test_cooldown_from_one_of_two_parallel_calls_blocks_a_third():
    """A 429 on one of two in-flight calls cools the slot down; the other call is
    unaffected and still completes and records normally via its own release."""
    pool = LLMPool(state_store=None, user_id=None)
    cfg = _cfg(parallel=2)
    await pool.add(cfg, "k")

    first = await pool.acquire(0)
    second = await pool.acquire(0)

    await pool.cool_down(first, 60)
    with pytest.raises(TimeoutError):
        await pool.acquire(0)

    # The second call completes independently and records normally.
    await pool.release(second)
    assert pool.state("p1").phase is LifecyclePhase.COOLING

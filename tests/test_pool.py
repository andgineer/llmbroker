"""Unit tests for LLMPool: slot invariants, key-resolution handling, and selection."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from llmbroker.broker.pool import LLMPool
from llmbroker.models import LifecyclePhase, LLMConfig
from llmbroker.optimizer import Optimizer


def _cfg(name="p1", *, parallel: int | None = None) -> LLMConfig:
    return LLMConfig(
        name=name, base_url="https://x/v1", model="m", api_key_ref="K", parallel=parallel
    )


async def test_add_new_registers_one_slot():
    pool = LLMPool()
    await pool.add(_cfg(), "key")
    assert len(pool) == 1
    assert "p1" in pool


async def test_add_existing_does_not_add_extra_slot():
    pool = LLMPool()
    await pool.add(_cfg(), "key")
    await pool.add(_cfg(), "key2")  # same name — update, no extra slot
    assert len(pool) == 1


async def test_add_none_key_preserves_existing_key():
    pool = LLMPool()
    await pool.add(_cfg(), "original")
    await pool.add(_cfg(), None)  # None means "leave key intact"
    assert pool.resolved_key("p1") == "original"


async def test_add_nonnone_key_overwrites_existing_key():
    pool = LLMPool()
    await pool.add(_cfg(), "old")
    await pool.add(_cfg(), "new")
    assert pool.resolved_key("p1") == "new"


async def test_add_with_none_key_for_new_entry_leaves_no_key():
    pool = LLMPool()
    await pool.add(_cfg(), None)
    assert not pool.has_key("p1")


async def test_add_keyless_is_never_available():
    """A config added without a resolved key stays visible but is never routable."""
    pool = LLMPool()
    await pool.add(_cfg(), None)
    assert "p1" in pool
    with pytest.raises(TimeoutError):
        await pool.acquire(0)


async def test_add_keyless_then_keyed_becomes_acquirable():
    """The keyless→keyed transition of an existing entry makes the slot acquirable."""
    pool = LLMPool()
    await pool.add(_cfg(), None)
    with pytest.raises(TimeoutError):
        await pool.acquire(0)
    await pool.add(_cfg(), "key")
    picked = await pool.acquire(0)
    assert picked.name == "p1"


async def test_drop_removes_config_and_key():
    pool = LLMPool()
    await pool.add(_cfg(), "key")
    await pool.drop("p1")
    assert "p1" not in pool
    assert not pool.has_key("p1")


async def test_drop_nonexistent_does_not_raise():
    pool = LLMPool()
    await pool.drop("ghost")  # must be silent


async def test_drop_clears_disabled_so_a_readded_config_is_routable():
    """Regression: drop() used to leave stale state behind, so a fresh config
    re-added under the same name silently inherited the old disabled latch forever."""
    pool = LLMPool()
    await pool.add(_cfg(), "key")
    pool.set_disabled("p1")
    await pool.drop("p1")

    await pool.add(_cfg(), "key")
    assert not pool.is_disabled("p1")
    picked = await pool.acquire(0)
    assert picked.name == "p1"


async def test_len_tracks_membership():
    pool = LLMPool()
    assert len(pool) == 0
    await pool.add(_cfg("a"), "k")
    await pool.add(_cfg("b"), "k")
    assert len(pool) == 2
    await pool.drop("a")
    assert len(pool) == 1


# --- phase / fail-count derivation (pool.state) ---


def test_fresh_llm_is_available():
    pool = LLMPool()
    s = pool.state("p1")
    assert s.phase is LifecyclePhase.AVAILABLE
    assert s.cooldown_until is None
    assert s.fail_count == 0


async def test_cooling_until_future_reports_cooling():
    pool = LLMPool()
    cfg = _cfg()
    await pool.add(cfg, "k")
    await pool.cool_down(cfg, 60)
    s = pool.state("p1")
    assert s.phase is LifecyclePhase.COOLING
    assert s.cooldown_until is not None
    assert s.fail_count == 1


async def test_cooling_in_past_reports_available():
    pool = LLMPool()
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
    pool = LLMPool()
    cfg = _cfg()
    await pool.add(cfg, "k")
    await pool.cool_down(cfg, 60)
    pool.clear_cooling("p1")
    assert pool.state("p1").phase is LifecyclePhase.AVAILABLE


async def test_mark_quality_fail_increments():
    pool = LLMPool()
    await pool.add(_cfg(), "k")
    pool.mark_quality_fail("p1")
    pool.mark_quality_fail("p1")
    assert pool.state("p1").fail_count == 2


# ---------------------------------------------------------------------------
# apply_peer_cooldowns — fed by the journal rebuild, never touches in_flight
# ---------------------------------------------------------------------------


async def test_apply_peer_cooldowns_raises_local_cooldown():
    pool = LLMPool()
    await pool.add(_cfg(), "k")
    until = datetime.now(UTC) + timedelta(seconds=60)

    await pool.apply_peer_cooldowns({"p1": until})

    assert pool.state("p1").phase is LifecyclePhase.COOLING
    assert pool.state("p1").cooldown_until == until


async def test_apply_peer_cooldowns_never_lowers_a_later_local_cooldown():
    pool = LLMPool()
    cfg = _cfg()
    await pool.add(cfg, "k")
    await pool.cool_down(cfg, 120)
    local_until = pool._slots["p1"].cooldown_until

    earlier = datetime.now(UTC) + timedelta(seconds=10)
    await pool.apply_peer_cooldowns({"p1": earlier})

    assert pool._slots["p1"].cooldown_until == local_until


async def test_apply_peer_cooldowns_ignores_unknown_slot():
    pool = LLMPool()
    await pool.apply_peer_cooldowns(
        {"ghost": datetime.now(UTC) + timedelta(seconds=60)}
    )  # no raise


async def test_apply_peer_cooldowns_never_touches_in_flight():
    pool = LLMPool()
    cfg = _cfg(parallel=2)
    await pool.add(cfg, "k")
    await pool.acquire(0)
    await pool.acquire(0)

    await pool.apply_peer_cooldowns({"p1": datetime.now(UTC) + timedelta(seconds=60)})

    assert pool._slots["p1"].in_flight == 2


async def test_apply_peer_cooldowns_folds_fail_count_as_max():
    pool = LLMPool()
    await pool.add(_cfg(), "k")
    pool._slots["p1"].fail_count = 2

    await pool.apply_peer_cooldowns({}, {"p1": 5})
    assert pool.state("p1").fail_count == 5

    await pool.apply_peer_cooldowns({}, {"p1": 1})  # peer lower than local — no regression
    assert pool.state("p1").fail_count == 5


# ---------------------------------------------------------------------------
# Manual disable (hard exclusion)
# ---------------------------------------------------------------------------


async def test_set_disabled_excludes_slot_immediately():
    pool = LLMPool()
    await pool.add(_cfg(), "k")
    pool.set_disabled("p1")
    assert pool.is_disabled("p1")
    with pytest.raises(TimeoutError):
        await pool.acquire(0)


async def test_disabled_config_in_configs_but_never_acquired_even_as_only_candidate():
    pool = LLMPool()
    await pool.add(_cfg(), "k")
    pool.set_disabled("p1")
    assert "p1" in pool.configs
    with pytest.raises(TimeoutError):
        await pool.acquire(0)


async def test_clear_disabled_readmits_slot():
    pool = LLMPool()
    await pool.add(_cfg(), "k")
    pool.set_disabled("p1")
    await pool.clear_disabled("p1")
    picked = await pool.acquire(0)
    assert picked.name == "p1"


async def test_clear_disabled_without_key_does_not_make_it_acquirable():
    pool = LLMPool()
    await pool.add(_cfg(), None)  # keyless, never routable in the first place
    pool.set_disabled("p1")
    await pool.clear_disabled("p1")
    with pytest.raises(TimeoutError):
        await pool.acquire(0)


async def test_add_while_disabled_does_not_make_it_acquirable():
    pool = LLMPool()
    await pool.add(_cfg(), None)
    pool.set_disabled("p1")
    await pool.add(_cfg(), "k")  # keyless -> keyed transition, but disabled
    with pytest.raises(TimeoutError):
        await pool.acquire(0)


async def test_disabled_mid_cooldown_stays_excluded_after_cooldown_expires():
    """A model can be mid-cooldown when it gets manually disabled — expiry must not
    resurrect it."""
    pool = LLMPool()
    cfg = _cfg()
    await pool.add(cfg, "k")
    acquired = await pool.acquire(0)

    await pool.cool_down(acquired, 30)
    pool.set_disabled("p1")
    pool._slots["p1"].cooldown_until = datetime.now(UTC) - timedelta(seconds=1)  # expire it

    with pytest.raises(TimeoutError):
        await pool.acquire(0)


async def test_release_of_disabled_config_leaves_it_excluded():
    pool = LLMPool()
    cfg = _cfg()
    await pool.add(cfg, "k")
    acquired = await pool.acquire(0)
    pool.set_disabled("p1")
    await pool.release(acquired)
    with pytest.raises(TimeoutError):
        await pool.acquire(0)


async def test_release_of_dropped_config_is_a_noop():
    pool = LLMPool()
    cfg = _cfg()
    await pool.add(cfg, "k")
    acquired = await pool.acquire(0)
    await pool.drop("p1")
    await pool.release(acquired)  # must not raise


# ---------------------------------------------------------------------------
# acquire(): curated order, demoted-last, waiting
# ---------------------------------------------------------------------------


async def test_acquire_wait_zero_raises_immediately_when_empty():
    pool = LLMPool()
    with pytest.raises(TimeoutError):
        await pool.acquire(0)


async def test_acquire_finite_wait_times_out_at_deadline():
    pool = LLMPool()
    await pool.add(_cfg(parallel=1), "k")
    await pool.acquire(0)  # occupy the only slot
    with pytest.raises(TimeoutError):
        await pool.acquire(0.05)


async def test_acquire_wait_none_wakes_when_cooldown_expires_without_a_timer():
    pool = LLMPool()
    cfg = _cfg()
    await pool.add(cfg, "k")
    acquired = await pool.acquire(0)
    await pool.cool_down(acquired, 0.05)

    picked = await asyncio.wait_for(pool.acquire(None), timeout=2.0)
    assert picked.name == "p1"


async def test_acquire_waiter_wakes_on_release():
    pool = LLMPool()
    cfg = _cfg(parallel=1)
    await pool.add(cfg, "k")
    acquired = await pool.acquire(0)

    waiter = asyncio.ensure_future(pool.acquire(None))
    await asyncio.sleep(0.01)
    assert not waiter.done()

    await pool.release(acquired)
    picked = await asyncio.wait_for(waiter, timeout=2.0)
    assert picked.name == "p1"


async def test_waiter_wakes_on_cooldown_expiry_with_inflight_sibling():
    pool = LLMPool()
    cfg = LLMConfig(name="a", base_url="u", model="m", api_key_ref="K")  # parallel=None
    await pool.add(cfg, "key")
    await pool.acquire(None)
    await pool.cool_down(cfg, 0.1)  # decrements in_flight back to 0
    pool._slots["a"].in_flight = 1  # emulate a sibling call still running
    picked = await asyncio.wait_for(pool.acquire(None), timeout=1.0)  # was: stalls
    assert picked.name == "a"


async def test_cooldown_expiry_alone_does_not_admit_a_slot_at_capacity():
    """Converse guard: with parallel=1, a busy sibling must still block acquisition
    even once the cooldown has expired."""
    pool = LLMPool()
    cfg = _cfg("a", parallel=1)
    await pool.add(cfg, "key")
    await pool.acquire(None)
    await pool.cool_down(cfg, 0.1)
    pool._slots["a"].in_flight = 1  # emulate a sibling call still running, at capacity
    with pytest.raises(TimeoutError):
        await pool.acquire(0.3)


async def test_curated_order_preferred_best_available_takes_all_traffic():
    """Curated priority: the best (lowest-order) available slot is picked every time,
    not round-robin — round-robin was removed."""
    pool = LLMPool()
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
    pool = LLMPool()
    cfg_a = _cfg("a")
    await pool.add(cfg_a, "k", order=0)
    await pool.add(_cfg("b"), "k", order=1)

    acquired_a = await pool.acquire(0)
    await pool.cool_down(acquired_a, 60)

    picked = await pool.acquire(0)
    assert picked.name == "b"


async def test_add_reasserts_curated_order_on_refresh():
    pool = LLMPool()
    await pool.add(_cfg("a"), "k", order=5)
    await pool.add(_cfg("b"), "k", order=1)
    picked = await pool.acquire(0)
    assert picked.name == "b"


async def test_pool_acquire_returns_only_available_slot():
    pool = LLMPool()
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
    pool = LLMPool(optimizer=opt)
    await pool.add(_cfg("bad"), "k", order=0)
    await pool.add(_cfg("good"), "k", order=1)

    picked = await pool.acquire(0, operation="summarize")
    assert picked.name == "good"


async def test_demoted_model_still_serves_when_no_alternative():
    opt = Optimizer(quality_min_count=10, quality_floor=0.3)
    for _ in range(10):
        opt.record_quality("bad", "summarize", 0.0)
    pool = LLMPool(optimizer=opt)
    await pool.add(_cfg("bad"), "k")

    picked = await pool.acquire(0, operation="summarize")
    assert picked.name == "bad"


async def test_demotion_is_per_operation_not_global():
    opt = Optimizer(quality_min_count=10, quality_floor=0.3)
    for _ in range(10):
        opt.record_quality("bad", "summarize", 0.0)
    pool = LLMPool(optimizer=opt)
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
    pool = LLMPool(optimizer=opt)
    await pool.add(_cfg("a"), "k")
    await pool.add(_cfg("b"), "k")

    picked = await pool.acquire(0, operation=None)
    assert picked.name in ("a", "b")


async def test_no_optimizer_never_demotes():
    pool = LLMPool()
    await pool.add(_cfg("a"), "k", order=0)
    picked = await pool.acquire(0, operation="summarize")
    assert picked.name == "a"


# ---------------------------------------------------------------------------
# Configurable per-LLM concurrency (parallel=None default is unlimited)
# ---------------------------------------------------------------------------


async def test_two_concurrent_acquires_of_one_llm_both_succeed_by_default():
    pool = LLMPool()
    cfg = _cfg()
    await pool.add(cfg, "k")

    first = await pool.acquire(0)
    second = await pool.acquire(0)
    assert first.name == "p1"
    assert second.name == "p1"
    assert pool._slots["p1"].in_flight == 2


async def test_parallel_one_serializes_like_today():
    """parallel=1 reproduces yesterday's one-in-flight-per-LLM behavior exactly."""
    pool = LLMPool()
    cfg = _cfg(parallel=1)
    await pool.add(cfg, "k")

    await pool.acquire(0)
    with pytest.raises(TimeoutError):
        await pool.acquire(0)


async def test_release_of_one_parallel_call_frees_exactly_one_slot():
    pool = LLMPool()
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
    pool = LLMPool()
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

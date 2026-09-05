"""Unit tests for LLMPool: slot invariants, payable filtering, and selection."""

import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest

from llmbroker.broker.pool import LLMPool
from llmbroker.exceptions import NoLLMAvailableError
from llmbroker.models import LifecyclePhase, LLMConfig
from llmbroker.optimizer import Optimizer


_PAYABLE = frozenset({"K"})


def _cfg(name="p1", *, parallel: int | None = None) -> LLMConfig:
    return LLMConfig(
        name=name, base_url="https://x/v1", model="m", api_key_ref="K", parallel=parallel
    )


async def test_add_new_registers_one_slot():
    pool = LLMPool()
    await pool.add(_cfg())
    assert len(pool) == 1
    assert "p1" in pool


async def test_add_existing_does_not_add_extra_slot():
    pool = LLMPool()
    await pool.add(_cfg())
    await pool.add(_cfg())  # same name — update, no extra slot
    assert len(pool) == 1


async def test_a_config_whose_ref_the_caller_cannot_pay_is_never_available():
    """A slot the caller holds no key for stays visible but is never routable — the
    pool holds no key of its own, so this is decided per acquisition."""
    pool = LLMPool()
    await pool.add(_cfg())
    assert "p1" in pool
    with pytest.raises(NoLLMAvailableError):
        await pool.acquire(time.monotonic(), payable=frozenset())


async def test_the_same_slot_is_acquirable_for_a_caller_that_can_pay():
    pool = LLMPool()
    await pool.add(_cfg())
    with pytest.raises(NoLLMAvailableError):
        await pool.acquire(time.monotonic(), payable=frozenset())
    picked = await pool.acquire(time.monotonic(), payable=_PAYABLE)
    assert picked.name == "p1"


async def test_drop_removes_the_config():
    pool = LLMPool()
    await pool.add(_cfg())
    await pool.drop("p1")
    assert "p1" not in pool


async def test_drop_nonexistent_does_not_raise():
    pool = LLMPool()
    await pool.drop("ghost")  # must be silent


async def test_drop_clears_disabled_so_a_readded_config_is_routable():
    """Regression: drop() used to leave stale state behind, so a fresh config
    re-added under the same name silently inherited the old disabled latch forever."""
    pool = LLMPool()
    await pool.add(_cfg())
    pool.set_disabled("p1")
    await pool.drop("p1")

    await pool.add(_cfg())
    assert not pool.is_disabled("p1")
    picked = await pool.acquire(time.monotonic(), payable=_PAYABLE)
    assert picked.name == "p1"


async def test_len_tracks_membership():
    pool = LLMPool()
    assert len(pool) == 0
    await pool.add(_cfg("a"))
    await pool.add(_cfg("b"))
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
    await pool.add(cfg)
    await pool.cool_down(cfg, 60)
    s = pool.state("p1")
    assert s.phase is LifecyclePhase.COOLING
    assert s.cooldown_until is not None
    assert s.fail_count == 1


async def test_cooling_in_past_reports_available():
    pool = LLMPool()
    cfg = _cfg()
    await pool.add(cfg)
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
    await pool.add(cfg)
    await pool.cool_down(cfg, 60)
    pool.clear_cooling("p1")
    assert pool.state("p1").phase is LifecyclePhase.AVAILABLE


# ---------------------------------------------------------------------------
# Manual disable (hard exclusion)
# ---------------------------------------------------------------------------


async def test_set_disabled_excludes_slot_immediately():
    pool = LLMPool()
    await pool.add(_cfg())
    pool.set_disabled("p1")
    assert pool.is_disabled("p1")
    with pytest.raises(NoLLMAvailableError):
        await pool.acquire(time.monotonic(), payable=_PAYABLE)


async def test_disabled_config_in_configs_but_never_acquired_even_as_only_candidate():
    pool = LLMPool()
    await pool.add(_cfg())
    pool.set_disabled("p1")
    assert "p1" in pool.configs
    with pytest.raises(NoLLMAvailableError):
        await pool.acquire(time.monotonic(), payable=_PAYABLE)


async def test_clear_disabled_readmits_slot():
    pool = LLMPool()
    await pool.add(_cfg())
    pool.set_disabled("p1")
    await pool.clear_disabled("p1")
    picked = await pool.acquire(time.monotonic(), payable=_PAYABLE)
    assert picked.name == "p1"


async def test_clear_disabled_without_a_payable_ref_does_not_make_it_acquirable():
    pool = LLMPool()
    await pool.add(_cfg())
    pool.set_disabled("p1")
    await pool.clear_disabled("p1")
    with pytest.raises(NoLLMAvailableError):
        await pool.acquire(time.monotonic(), payable=frozenset())


async def test_add_while_disabled_does_not_make_it_acquirable():
    pool = LLMPool()
    await pool.add(_cfg())
    pool.set_disabled("p1")
    await pool.add(_cfg())  # re-added, but the disabled latch survives the upsert
    with pytest.raises(NoLLMAvailableError):
        await pool.acquire(time.monotonic(), payable=_PAYABLE)


async def test_disabled_mid_cooldown_stays_excluded_after_cooldown_expires():
    """A model can be mid-cooldown when it gets manually disabled — expiry must not
    resurrect it."""
    pool = LLMPool()
    cfg = _cfg()
    await pool.add(cfg)
    acquired = await pool.acquire(time.monotonic(), payable=_PAYABLE)

    await pool.cool_down(acquired, 30)
    pool.set_disabled("p1")
    pool._slots["p1"].cooldown_until = datetime.now(UTC) - timedelta(seconds=1)  # expire it

    with pytest.raises(NoLLMAvailableError):
        await pool.acquire(time.monotonic(), payable=_PAYABLE)


async def test_release_of_disabled_config_leaves_it_excluded():
    pool = LLMPool()
    cfg = _cfg()
    await pool.add(cfg)
    acquired = await pool.acquire(time.monotonic(), payable=_PAYABLE)
    pool.set_disabled("p1")
    await pool.release(acquired)
    with pytest.raises(NoLLMAvailableError):
        await pool.acquire(time.monotonic(), payable=_PAYABLE)


async def test_release_of_dropped_config_is_a_noop():
    pool = LLMPool()
    cfg = _cfg()
    await pool.add(cfg)
    acquired = await pool.acquire(time.monotonic(), payable=_PAYABLE)
    await pool.drop("p1")
    await pool.release(acquired)  # must not raise


# ---------------------------------------------------------------------------
# acquire(): curated order, demoted-last, waiting
# ---------------------------------------------------------------------------


async def test_acquire_wait_zero_raises_immediately_when_empty():
    pool = LLMPool()
    with pytest.raises(NoLLMAvailableError):
        await pool.acquire(time.monotonic(), payable=_PAYABLE)


async def test_acquire_finite_wait_times_out_at_deadline():
    pool = LLMPool()
    await pool.add(_cfg(parallel=1))
    await pool.acquire(time.monotonic(), payable=_PAYABLE)  # occupy the only slot
    with pytest.raises(NoLLMAvailableError):
        await pool.acquire(time.monotonic() + 0.05, payable=_PAYABLE)


async def test_acquire_wait_none_wakes_when_cooldown_expires_without_a_timer():
    pool = LLMPool()
    cfg = _cfg()
    await pool.add(cfg)
    acquired = await pool.acquire(time.monotonic(), payable=_PAYABLE)
    await pool.cool_down(acquired, 0.05)

    picked = await asyncio.wait_for(pool.acquire(None, payable=_PAYABLE), timeout=2.0)
    assert picked.name == "p1"


async def test_acquire_waiter_wakes_on_release():
    pool = LLMPool()
    cfg = _cfg(parallel=1)
    await pool.add(cfg)
    acquired = await pool.acquire(time.monotonic(), payable=_PAYABLE)

    waiter = asyncio.ensure_future(pool.acquire(None, payable=_PAYABLE))
    await asyncio.sleep(0.01)
    assert not waiter.done()

    await pool.release(acquired)
    picked = await asyncio.wait_for(waiter, timeout=2.0)
    assert picked.name == "p1"


async def test_waiter_wakes_on_cooldown_expiry_with_inflight_sibling():
    pool = LLMPool()
    cfg = LLMConfig(name="a", base_url="u", model="m", api_key_ref="K")  # parallel=None
    await pool.add(cfg)
    await pool.acquire(None, payable=_PAYABLE)
    await pool.cool_down(cfg, 0.1)  # decrements in_flight back to 0
    pool._slots["a"].in_flight = 1  # emulate a sibling call still running
    picked = await asyncio.wait_for(
        pool.acquire(None, payable=_PAYABLE), timeout=1.0
    )  # was: stalls
    assert picked.name == "a"


async def test_cooldown_expiry_alone_does_not_admit_a_slot_at_capacity():
    """Converse guard: with parallel=1, a busy sibling must still block acquisition
    even once the cooldown has expired."""
    pool = LLMPool()
    cfg = _cfg("a", parallel=1)
    await pool.add(cfg)
    await pool.acquire(None, payable=_PAYABLE)
    await pool.cool_down(cfg, 0.1)
    pool._slots["a"].in_flight = 1  # emulate a sibling call still running, at capacity
    with pytest.raises(NoLLMAvailableError):
        await pool.acquire(time.monotonic() + 0.3, payable=_PAYABLE)


async def test_curated_order_preferred_best_available_takes_all_traffic():
    """Curated priority: the best (lowest-order) available slot is picked every time,
    not round-robin — round-robin was removed."""
    pool = LLMPool()
    await pool.add(_cfg("a"), order=0)
    await pool.add(_cfg("b"), order=1)
    await pool.add(_cfg("c"), order=2)

    picked_names = []
    for _ in range(4):
        picked = await pool.acquire(time.monotonic(), payable=_PAYABLE)
        picked_names.append(picked.name)
        await pool.release(picked)

    assert picked_names == ["a", "a", "a", "a"]


async def test_curated_order_falls_back_to_next_when_best_is_cooling():
    pool = LLMPool()
    cfg_a = _cfg("a")
    await pool.add(cfg_a, order=0)
    await pool.add(_cfg("b"), order=1)

    acquired_a = await pool.acquire(time.monotonic(), payable=_PAYABLE)
    await pool.cool_down(acquired_a, 60)

    picked = await pool.acquire(time.monotonic(), payable=_PAYABLE)
    assert picked.name == "b"


async def test_add_reasserts_curated_order_on_refresh():
    pool = LLMPool()
    await pool.add(_cfg("a"), order=5)
    await pool.add(_cfg("b"), order=1)
    picked = await pool.acquire(time.monotonic(), payable=_PAYABLE)
    assert picked.name == "b"


async def test_pool_acquire_returns_only_available_slot():
    pool = LLMPool()
    a = _cfg("a")
    await pool.add(a)
    result = await pool.acquire(time.monotonic(), payable=_PAYABLE)
    assert result is a


# ---------------------------------------------------------------------------
# Demoted-last selection (Optimizer.is_demoted feeds the sort key)
# ---------------------------------------------------------------------------


async def test_demoted_for_operation_sorts_last_while_alternative_exists():
    opt = Optimizer(quality_min_count=10, quality_floor=0.3)
    for i in range(10):
        opt.record_quality("bad", "summarize", f"c{i}", 0.0)
    pool = LLMPool(optimizer=opt)
    await pool.add(_cfg("bad"), order=0)
    await pool.add(_cfg("good"), order=1)

    picked = await pool.acquire(time.monotonic(), payable=_PAYABLE, operation="summarize")
    assert picked.name == "good"


async def test_demoted_model_still_serves_when_no_alternative():
    opt = Optimizer(quality_min_count=10, quality_floor=0.3)
    for i in range(10):
        opt.record_quality("bad", "summarize", f"c{i}", 0.0)
    pool = LLMPool(optimizer=opt)
    await pool.add(_cfg("bad"))

    picked = await pool.acquire(time.monotonic(), payable=_PAYABLE, operation="summarize")
    assert picked.name == "bad"


async def test_demotion_is_per_operation_not_global():
    opt = Optimizer(quality_min_count=10, quality_floor=0.3)
    for i in range(10):
        opt.record_quality("bad", "summarize", f"c{i}", 0.0)
    pool = LLMPool(optimizer=opt)
    await pool.add(_cfg("bad"))

    picked = await pool.acquire(time.monotonic(), payable=_PAYABLE, operation="translate")
    assert picked.name == "bad"


async def test_every_model_demoted_pool_still_serves():
    """A rater that scores everything low demotes everything — the pool keeps
    operating on curated order within the demoted set, never goes empty."""
    opt = Optimizer(quality_min_count=10, quality_floor=0.3)
    for i in range(10):
        opt.record_quality("a", None, f"c{i}", 0.0)
        opt.record_quality("b", None, f"c{i}", 0.0)
    pool = LLMPool(optimizer=opt)
    await pool.add(_cfg("a"))
    await pool.add(_cfg("b"))

    picked = await pool.acquire(time.monotonic(), payable=_PAYABLE, operation=None)
    assert picked.name in ("a", "b")


async def test_no_optimizer_never_demotes():
    pool = LLMPool()
    await pool.add(_cfg("a"), order=0)
    picked = await pool.acquire(time.monotonic(), payable=_PAYABLE, operation="summarize")
    assert picked.name == "a"


# ---------------------------------------------------------------------------
# Configurable per-LLM concurrency (parallel=None default is unlimited)
# ---------------------------------------------------------------------------


async def test_two_concurrent_acquires_of_one_llm_both_succeed_by_default():
    pool = LLMPool()
    cfg = _cfg()
    await pool.add(cfg)

    first = await pool.acquire(time.monotonic(), payable=_PAYABLE)
    second = await pool.acquire(time.monotonic(), payable=_PAYABLE)
    assert first.name == "p1"
    assert second.name == "p1"
    assert pool._slots["p1"].in_flight == 2


async def test_parallel_one_serializes_like_today():
    """parallel=1 reproduces yesterday's one-in-flight-per-LLM behavior exactly."""
    pool = LLMPool()
    cfg = _cfg(parallel=1)
    await pool.add(cfg)

    await pool.acquire(time.monotonic(), payable=_PAYABLE)
    with pytest.raises(NoLLMAvailableError):
        await pool.acquire(time.monotonic(), payable=_PAYABLE)


async def test_release_of_one_parallel_call_frees_exactly_one_slot():
    pool = LLMPool()
    cfg = _cfg(parallel=2)
    await pool.add(cfg)

    first = await pool.acquire(time.monotonic(), payable=_PAYABLE)
    await pool.acquire(time.monotonic(), payable=_PAYABLE)
    with pytest.raises(NoLLMAvailableError):
        await pool.acquire(time.monotonic(), payable=_PAYABLE)

    await pool.release(first)
    picked = await pool.acquire(time.monotonic(), payable=_PAYABLE)
    assert picked.name == "p1"


async def test_cooldown_from_one_of_two_parallel_calls_blocks_a_third():
    """A 429 on one of two in-flight calls cools the slot down; the other call is
    unaffected and still completes and records normally via its own release."""
    pool = LLMPool()
    cfg = _cfg(parallel=2)
    await pool.add(cfg)

    first = await pool.acquire(time.monotonic(), payable=_PAYABLE)
    second = await pool.acquire(time.monotonic(), payable=_PAYABLE)

    await pool.cool_down(first, 60)
    with pytest.raises(NoLLMAvailableError):
        await pool.acquire(time.monotonic(), payable=_PAYABLE)

    # The second call completes independently and records normally.
    await pool.release(second)
    assert pool.state("p1").phase is LifecyclePhase.COOLING


# ---------------------------------------------------------------------------
# acquire_many(): distinct slots, and the recovery claim
# ---------------------------------------------------------------------------


async def _expired(pool: LLMPool, name: str) -> None:
    cfg = pool.config(name)
    await pool.cool_down(cfg, 60)
    pool._slots[name].cooldown_until = datetime.now(UTC) - timedelta(seconds=1)


async def test_acquire_many_takes_distinct_slots_in_curated_order():
    pool = LLMPool()
    for order, name in enumerate(("a", "b", "c")):
        await pool.add(_cfg(name), order)
    taken = await pool.acquire_many(time.monotonic(), payable=_PAYABLE, width=2)
    assert [cfg.name for cfg in taken] == ["a", "b"]


async def test_acquire_many_returns_fewer_than_asked_rather_than_waiting():
    pool = LLMPool()
    await pool.add(_cfg("a"))
    taken = await pool.acquire_many(time.monotonic(), payable=_PAYABLE, width=3)
    assert [cfg.name for cfg in taken] == ["a"]


async def test_recovery_width_only_widens_a_post_cooldown_claim():
    pool = LLMPool()
    for order, name in enumerate(("a", "b")):
        await pool.add(_cfg(name), order)
    healthy = await pool.acquire_many(time.monotonic(), payable=_PAYABLE, recovery_width=2)
    assert [cfg.name for cfg in healthy] == ["a"]
    await pool.release(healthy[0])

    await _expired(pool, "a")
    recovering = await pool.acquire_many(time.monotonic(), payable=_PAYABLE, recovery_width=2)
    assert [cfg.name for cfg in recovering] == ["a", "b"]


async def test_a_claimed_recovery_is_exclusive_whatever_its_capacity():
    """``parallel`` is provider capacity: it may not let a second caller make the
    unprotected post-cooldown call the claim exists to cover."""
    pool = LLMPool()
    await pool.add(_cfg("a"), 0)  # parallel=None — unlimited
    await pool.add(_cfg("b"), 1)
    await _expired(pool, "a")

    first = await pool.acquire(time.monotonic(), payable=_PAYABLE)
    assert first.name == "a"
    second = await pool.acquire(time.monotonic(), payable=_PAYABLE)
    assert second.name == "b"


async def test_a_recovery_released_unsettled_is_due_again():
    pool = LLMPool()
    await pool.add(_cfg("a"))
    await _expired(pool, "a")
    claimed = await pool.acquire(time.monotonic(), payable=_PAYABLE)
    await pool.release(claimed)
    again = await pool.acquire_many(time.monotonic(), payable=_PAYABLE, recovery_width=2)
    assert pool._slots["a"].recovery_claimed
    assert [cfg.name for cfg in again] == ["a"]


async def test_a_recovery_that_answered_owes_no_second_one():
    pool = LLMPool()
    for order, name in enumerate(("a", "b")):
        await pool.add(_cfg(name), order)
    await _expired(pool, "a")
    claimed = await pool.acquire(time.monotonic(), payable=_PAYABLE)
    await pool.release(claimed)
    pool.clear_cooling("a")
    assert [
        cfg.name
        for cfg in await pool.acquire_many(time.monotonic(), payable=_PAYABLE, recovery_width=2)
    ] == ["a"]


async def test_a_recovery_that_failed_again_is_due_on_the_new_cooldown():
    pool = LLMPool()
    await pool.add(_cfg("a"))
    await _expired(pool, "a")
    claimed = await pool.acquire(time.monotonic(), payable=_PAYABLE)
    await pool.cool_down(claimed, 60)
    slot = pool._slots["a"]
    assert (slot.recovery_due, slot.recovery_claimed) == (True, False)


async def test_take_free_never_waits_and_never_raises():
    pool = LLMPool()
    await pool.add(_cfg("a", parallel=1))
    assert await pool.take_free(payable=_PAYABLE, width=2)
    assert await pool.take_free(payable=_PAYABLE, width=2) == []
    assert await pool.take_free(payable=frozenset(), width=1) == []

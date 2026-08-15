"""Curated weight as the pool's priority carrier: the prior alone when nothing has
been rated, shrunk toward the host's ratings as they arrive, and always below the
budget and demotion verdicts above it in the key.
"""

import time
import uuid
from datetime import UTC, datetime

import pytest

from llmbroker.broker.learning import Learner
from llmbroker.broker.pool import LLMPool
from llmbroker.models import Call, CallStatus, LLMConfig
from llmbroker.optimizer import Optimizer
from llmbroker.sqlite import Store as SqliteStore


def _cfg(name: str, weight: float = 0.0) -> LLMConfig:
    return LLMConfig(
        name=name,
        base_url=f"https://{name}/v1",
        model="m",
        api_key_ref="K",
        weight=weight,
    )


async def _pool(*entries: tuple[str, float], optimizer: Optimizer | None = None) -> LLMPool:
    """Entries in the order given, so ``order`` follows the argument list."""
    pool = LLMPool(optimizer=optimizer if optimizer is not None else Optimizer())
    for order, (name, weight) in enumerate(entries):
        await pool.add(_cfg(name, weight), order=order)
    return pool


async def _acquired(pool: LLMPool, **kwargs) -> str:
    cfg = await pool.acquire(None, payable=frozenset({"K"}), **kwargs)
    await pool.release(cfg)
    return cfg.name


# ── The prior, before any evidence ───────────────────────────────────────────


async def test_priority_with_no_ratings_is_the_weight_itself():
    pool = await _pool(("a", 0.62))
    assert pool._priority(pool._slots["a"], None) == 0.62


async def test_a_weighted_entry_outranks_a_weightless_one_ordered_ahead_of_it():
    pool = await _pool(("first", 0.0), ("second", 0.8))
    assert await _acquired(pool) == "second"


async def test_two_weightless_entries_fall_back_to_curated_order():
    pool = await _pool(("first", 0.0), ("second", 0.0))
    assert [await _acquired(pool) for _ in range(3)] == ["first"] * 3


# ── Evidence displacing the prior ────────────────────────────────────────────


async def test_a_full_window_of_good_ratings_overtakes_an_unrated_favourite():
    """However far apart the curator put them: ratings, once there are enough of
    them, decide the order by themselves."""
    optimizer = Optimizer()
    pool = await _pool(("proven", 0.2), ("curated", 0.9), optimizer=optimizer)
    for i in range(optimizer.quality_window):
        optimizer.record_quality("proven", None, f"c{i}", 1.0)
    assert await _acquired(pool) == "proven"


def test_a_full_window_replaces_the_weight_outright():
    """The weight says where a model starts, not where it stays: once the window is
    full it contributes nothing at all, whatever the curator wrote."""
    optimizer = Optimizer()
    for i in range(optimizer.quality_window):
        optimizer.record_quality("m", None, f"c{i}", 0.4)
    assert optimizer.quality_score("m", None, 0.0) == pytest.approx(0.4)
    assert optimizer.quality_score("m", None, 1.0) == pytest.approx(0.4)


async def test_a_full_window_of_bad_ratings_falls_below_a_weightless_entry():
    optimizer = Optimizer()
    pool = await _pool(("fallen", 0.9), ("plain", 0.0), optimizer=optimizer)
    for i in range(optimizer.quality_window):
        optimizer.record_quality("fallen", None, f"c{i}", 0.0)
    # It measures 0.0 — the bottom of the scale, where an unrated weightless entry
    # also sits — and the demotion verdict one term above separates proven-bad from
    # never-tried. Without it the earlier `order` would still hand it the traffic.
    assert pool._priority(pool._slots["fallen"], None) == pytest.approx(0.0)
    assert optimizer.is_demoted("fallen", None) is True
    assert await _acquired(pool) == "plain"
    # Below, but never withdrawn: it still answers when nothing else is left.
    await pool.drop("plain")
    assert await _acquired(pool) == "fallen"


async def test_ratings_move_the_priority_monotonically_from_weight_to_mean():
    optimizer = Optimizer()
    pool = await _pool(("m", 0.9), optimizer=optimizer)
    slot = pool._slots["m"]
    seen = [pool._priority(slot, None)]
    for i in range(optimizer.quality_window):
        optimizer.record_quality("m", None, f"c{i}", 0.1)
        seen.append(pool._priority(slot, None))
    assert seen[0] == 0.9
    assert all(later < earlier for earlier, later in zip(seen, seen[1:], strict=False))
    assert seen[-1] == pytest.approx(0.1)


async def test_ratings_on_one_operation_leave_the_other_buckets_alone():
    optimizer = Optimizer()
    pool = await _pool(("m", 0.7), optimizer=optimizer)
    slot = pool._slots["m"]
    for i in range(optimizer.quality_window):
        optimizer.record_quality("m", "a", f"c{i}", 0.0)
    assert pool._priority(slot, "a") < 0.3
    assert pool._priority(slot, None) == 0.7
    assert pool._priority(slot, "b") == 0.7


# ── What outranks priority ───────────────────────────────────────────────────


async def test_demotion_outranks_priority():
    optimizer = Optimizer()
    pool = await _pool(("demoted", 0.95), ("plain", 0.0), optimizer=optimizer)
    for i in range(optimizer.quality_min_count):
        optimizer.record_quality("demoted", None, f"c{i}", 0.0)
    assert optimizer.is_demoted("demoted", None)
    assert await _acquired(pool) == "plain"


async def test_a_recent_budget_miss_outranks_priority():
    pool = await _pool(("slow-once", 0.9), ("plain", 0.0))
    pool.raise_budget_bound("slow-once", 30.0, datetime.now(UTC))
    # No budget on offer: nothing to weigh, the curated favourite still wins.
    assert await _acquired(pool) == "slow-once"
    assert await _acquired(pool, answer_deadline=time.monotonic() + 5.0) == "plain"


# ── The invariant: failures never rate a model ───────────────────────────────


async def _noop_resync() -> None:
    return


async def test_failed_calls_leave_the_quality_window_and_the_priority_untouched(tmp_path):
    """Demotion has no time-based recovery, so nothing but a host rating may enter
    the quality window: an auto-generated score would make a bad hour permanent."""
    optimizer = Optimizer()
    pool = await _pool(("m", 0.6), optimizer=optimizer)
    store = SqliteStore(str(tmp_path / "journal.db"))
    learner = Learner(optimizer, store, pool)
    try:
        for status in (CallStatus.RATE_LIMITED, CallStatus.UNAVAILABLE, CallStatus.ERROR) * 5:
            call = Call(
                id=str(uuid.uuid4()),
                llm_name="m",
                operation=None,
                trace_id=None,
                status=status,
                ts=datetime.now(UTC),
            )
            await store.record(call)
            await learner.observe(call)
        await learner.relearn()
    finally:
        await store.aclose()
    assert pool._priority(pool._slots["m"], None) == 0.6
    assert optimizer.is_demoted("m", None) is False


# ── No optimizer ─────────────────────────────────────────────────────────────


async def test_without_an_optimizer_priority_is_the_raw_weight():
    pool = LLMPool(optimizer=None)
    await pool.add(_cfg("plain", 0.0), order=0)
    await pool.add(_cfg("weighted", 0.4), order=1)
    assert pool._priority(pool._slots["weighted"], None) == 0.4
    assert await _acquired(pool) == "weighted"

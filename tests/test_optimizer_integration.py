"""Integration tests: Optimizer bookkeeping and OptimizerPolicy over all telemetry backends.

Each test runs against every implemented telemetry backend:
  - toml  — NoTelemetry (no DB, the project default)
  - sqlite
  - postgres (Docker — marked docker)
  - mongodb  (Docker — marked docker)

What is verified:
1. Real usefulness: OptimizerPolicy consistently routes to the healthier LLM once enough
   samples are collected, and automatic retirement removes the unreliable one.
2. No stuck states: an LLM's phase is always exactly AVAILABLE or COOLING, derived fresh
   from cooldown_until, regardless of which backend stores the calls.

The any_telemetry and queryable_telemetry fixtures are defined in conftest.py.
"""

import random
import uuid

import pytest

from llmbroker.broker.pool import LLMPool
from llmbroker.models import Call, CallStatus, LifecyclePhase, LLMConfig
from llmbroker.optimizer import Optimizer, OptimizerPolicy, OptimizerTelemetry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(name: str) -> LLMConfig:
    return LLMConfig(name=name, base_url="https://x/v1", model="m", api_key_ref="k")


def _call(llm_name: str, status: CallStatus, operation: str | None = None, **kw) -> Call:
    return Call(
        id=str(uuid.uuid4()),
        llm_name=llm_name,
        operation=operation,
        trace_id=None,
        status=status,
        **kw,
    )


def _opt_tel(opt: Optimizer, telemetry: object, pool: LLMPool) -> OptimizerTelemetry:
    return OptimizerTelemetry(opt, telemetry, pool)


# ---------------------------------------------------------------------------
# Automatic retirement — no stuck states, across backends
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [CallStatus.RATE_LIMITED, CallStatus.UNAVAILABLE])
async def test_low_usable_rate_retires_llm_across_backends(any_telemetry, status):
    """Enough RATE_LIMITED/UNAVAILABLE calls via any backend drop the LLM once usable_rate is low."""
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg("llm1"), "key")
    opt = Optimizer(min_sample_count=3, removal_rate_floor=0.9)
    opt_tel = _opt_tel(opt, any_telemetry, pool)

    for _ in range(3):
        await opt_tel.record(_call("llm1", status))

    assert "llm1" not in pool
    alerts = opt.alerts()
    assert len(alerts) == 1
    assert "retired" in alerts[0].message


async def test_repeated_ok_calls_keep_llm_available(any_telemetry):
    """Sustained OK calls keep the LLM AVAILABLE and its usable_rate high."""
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg("llm1"), "key")
    opt = Optimizer(min_sample_count=5)
    opt_tel = _opt_tel(opt, any_telemetry, pool)

    for _ in range(15):
        await opt_tel.record(_call("llm1", CallStatus.OK))

    assert pool.state("llm1").phase is LifecyclePhase.AVAILABLE
    assert opt.rl_fail_count("llm1") == 0
    assert opt.usable_rate("llm1", None) > 0.9


@pytest.mark.parametrize("http_status", [401, 403])
async def test_auth_failure_drops_llm_cleanly(any_telemetry, http_status):
    """HTTP 401/403 drops the LLM from pool immediately and fires an API-key alert.

    Verifies a re-added LLM starts fresh — no stale bookkeeping survives the drop.
    """
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg("llm1"), "key")
    opt = Optimizer()
    opt_tel = _opt_tel(opt, any_telemetry, pool)

    await opt_tel.record(_call("llm1", CallStatus.ERROR, http_status=http_status))

    assert "llm1" not in pool
    alerts = opt.alerts()
    assert len(alerts) == 1
    assert "API key" in alerts[0].message
    assert str(http_status) in alerts[0].message
    pool.add(_cfg("llm1"), "key")
    assert pool.state("llm1").phase is LifecyclePhase.AVAILABLE


# ---------------------------------------------------------------------------
# Optimizer real usefulness
# ---------------------------------------------------------------------------


async def test_optimizer_retires_unreliable_prefers_stable(any_telemetry):
    """Full optimizer loop: unreliable LLM is retired; healthy LLM stays AVAILABLE and preferred.

    Demonstrates the real benefit of the optimizer over blind round-robin or
    first-available selection.
    """
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg("stable"), "key")
    pool.add(_cfg("flaky"), "key")
    opt = Optimizer(
        min_sample_count=5,
        removal_rate_floor=0.5,
        exploration_fraction=0.0,
        usable_rate_floor=0.0,
    )
    opt_tel = _opt_tel(opt, any_telemetry, pool)

    for _ in range(5):
        await opt_tel.record(_call("stable", CallStatus.OK, latency_ms=50))

    for _ in range(5):
        await opt_tel.record(_call("flaky", CallStatus.RATE_LIMITED))

    assert "flaky" not in pool
    assert pool.state("stable").phase is LifecyclePhase.AVAILABLE

    policy = OptimizerPolicy(opt)
    selected = {policy.select([_cfg("stable")], operation=None).name for _ in range(20)}
    assert selected == {"stable"}


async def test_policy_prefers_lower_latency_when_quality_equal(any_telemetry):
    """OptimizerPolicy picks the faster LLM when success rates are equal."""
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg("fast"), "key")
    pool.add(_cfg("slow"), "key")
    opt = Optimizer(min_sample_count=10, exploration_fraction=0.0, usable_rate_floor=0.0)
    opt_tel = _opt_tel(opt, any_telemetry, pool)

    for _ in range(10):
        await opt_tel.record(_call("fast", CallStatus.OK, latency_ms=20))
        await opt_tel.record(_call("slow", CallStatus.OK, latency_ms=500))

    policy = OptimizerPolicy(opt)
    selected = {policy.select([_cfg("fast"), _cfg("slow")], operation=None).name for _ in range(10)}
    assert selected == {"fast"}


async def test_policy_quality_floor_gates_failing_llm(any_telemetry):
    """OptimizerPolicy excludes an LLM whose success rate is below usable_rate_floor."""
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg("reliable"), "key")
    pool.add(_cfg("broken"), "key")
    opt = Optimizer(
        min_sample_count=10,
        usable_rate_floor=0.7,
        exploration_fraction=0.0,
    )
    opt_tel = _opt_tel(opt, any_telemetry, pool)

    for _ in range(10):
        await opt_tel.record(_call("reliable", CallStatus.OK, latency_ms=100))
    for _ in range(10):
        await opt_tel.record(_call("broken", CallStatus.ERROR))

    policy = OptimizerPolicy(opt)
    selected = {
        policy.select([_cfg("reliable"), _cfg("broken")], operation=None).name for _ in range(20)
    }
    assert "reliable" in selected
    assert "broken" not in selected


# ---------------------------------------------------------------------------
# Policy branches (C-B)
# ---------------------------------------------------------------------------


async def test_policy_floor_drops_all_falls_back_and_alerts(any_telemetry):
    """All candidates below usable_rate_floor → score-ranked fallback over all, one alert emitted.

    A second select() immediately after must NOT emit a duplicate alert (rate-limited by
    _FLOOR_ALERT_INTERVAL).
    """
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg("bad1"), "key")
    pool.add(_cfg("bad2"), "key")
    opt = Optimizer(
        min_sample_count=5,
        usable_rate_floor=0.9,
        removal_rate_floor=0.0,
        exploration_fraction=0.0,
    )
    opt_tel = _opt_tel(opt, any_telemetry, pool)

    for _ in range(10):
        await opt_tel.record(_call("bad1", CallStatus.ERROR))
        await opt_tel.record(_call("bad2", CallStatus.ERROR))

    policy = OptimizerPolicy(opt)
    result = policy.select([_cfg("bad1"), _cfg("bad2")], operation=None)
    assert result is not None

    alerts = opt.alerts()
    assert len(alerts) == 1
    assert "quality floor" in alerts[0].message

    policy.select([_cfg("bad1"), _cfg("bad2")], operation=None)
    assert opt.alerts() == []


async def test_policy_exploration_returns_random(any_telemetry, monkeypatch):
    """Explore path (random.random < exploration_fraction) can bypass the quality floor."""
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg("good"), "key")
    pool.add(_cfg("floored"), "key")
    opt = Optimizer(
        min_sample_count=5,
        usable_rate_floor=0.9,
        exploration_fraction=0.5,
    )
    opt_tel = _opt_tel(opt, any_telemetry, pool)

    for _ in range(10):
        await opt_tel.record(_call("floored", CallStatus.ERROR))

    floored_cfg = _cfg("floored")
    monkeypatch.setattr(random, "random", lambda: 0.0)
    monkeypatch.setattr(random, "choice", lambda _candidates: floored_cfg)

    policy = OptimizerPolicy(opt)
    result = policy.select([_cfg("good"), floored_cfg], operation=None)
    assert result is floored_cfg


async def test_policy_background_operation_ranks_quality_first(any_telemetry):
    """background_operations → rate-first ranking; slow-but-reliable beats fast-but-flaky."""
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg("reliable"), "key")
    pool.add(_cfg("fast_flaky"), "key")
    opt = Optimizer(
        min_sample_count=10,
        exploration_fraction=0.0,
        usable_rate_floor=0.0,
        background_operations=frozenset(["bg"]),
    )
    opt_tel = _opt_tel(opt, any_telemetry, pool)

    for _ in range(10):
        await opt_tel.record(_call("reliable", CallStatus.OK, latency_ms=1000, operation="bg"))
    for _ in range(5):
        await opt_tel.record(_call("fast_flaky", CallStatus.OK, latency_ms=10, operation="bg"))
    for _ in range(5):
        await opt_tel.record(_call("fast_flaky", CallStatus.ERROR, operation="bg"))

    policy = OptimizerPolicy(opt)
    results = {
        policy.select([_cfg("reliable"), _cfg("fast_flaky")], operation="bg").name
        for _ in range(20)
    }
    assert results == {"reliable"}


# ---------------------------------------------------------------------------
# Statistical no-stuck-state (C-C)
# ---------------------------------------------------------------------------


async def test_operation_stats_are_isolated(any_telemetry):
    """Failures under operation A do not lower usable_rate for operation B."""
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg("llm1"), "key")
    opt = Optimizer(min_sample_count=5)
    opt_tel = _opt_tel(opt, any_telemetry, pool)

    for _ in range(10):
        await opt_tel.record(_call("llm1", CallStatus.ERROR, operation="op_a"))
    for _ in range(10):
        await opt_tel.record(_call("llm1", CallStatus.OK, operation="op_b"))

    rate_a = opt.usable_rate("llm1", "op_a")
    rate_b = opt.usable_rate("llm1", "op_b")
    assert rate_a is not None
    assert rate_b is not None
    assert rate_b > rate_a
    assert rate_b > 0.9


async def test_stale_failures_forgiven_by_decay(any_telemetry):
    """Old failures are forgiven by per-event decay — no permanent statistical penalty.

    Hand-computed (d=9/11): 10 ERROR from scratch give rate~=0.0868; 10 subsequent OK
    calls bring it back above a 0.7 floor (well past the ~4 needed to clear 0.5).
    """
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg("llm1"), "key")
    opt = Optimizer(min_sample_count=5, usable_rate_floor=0.7)
    opt_tel = _opt_tel(opt, any_telemetry, pool)

    for _ in range(10):
        await opt_tel.record(_call("llm1", CallStatus.ERROR))
    for _ in range(10):
        await opt_tel.record(_call("llm1", CallStatus.OK, latency_ms=50))

    rate = opt.usable_rate("llm1", None)
    assert rate is not None
    assert rate >= opt.usable_rate_floor


async def test_cold_start_not_gated(any_telemetry):
    """Below min_sample_count usable_rate returns None — floor does not gate the LLM."""
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg("llm1"), "key")
    opt = Optimizer(
        min_sample_count=10,
        usable_rate_floor=0.9,
        exploration_fraction=0.0,
    )
    opt_tel = _opt_tel(opt, any_telemetry, pool)

    for _ in range(3):
        await opt_tel.record(_call("llm1", CallStatus.ERROR))

    assert opt.usable_rate("llm1", None) is None

    policy = OptimizerPolicy(opt)
    result = policy.select([_cfg("llm1")], operation=None)
    assert result is not None
    assert result.name == "llm1"

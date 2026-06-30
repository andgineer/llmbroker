"""Integration tests: optimizer FSM and OptimizerPolicy over all telemetry backends.

Each test runs against every implemented telemetry backend:
  - toml  — NoTelemetry (no DB, the project default)
  - sqlite
  - postgres (Docker — marked docker)
  - mongodb  (Docker — marked docker)

What is verified:
1. Real usefulness: OptimizerPolicy consistently routes to the healthier LLM once enough
   samples are collected, and circuit-breaks the unreliable one.
2. No stuck states: LLMs transition correctly between OFFLINE → PROBING → AVAILABLE and
   never remain frozen in the wrong phase regardless of which backend stores the calls.
3. Warm-start (queryable backends): seed_from_metrics correctly primes the adaptive delay
   from persisted call history across all queryable backends.

The any_telemetry and queryable_telemetry fixtures are defined in conftest.py.
"""

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


def _call(llm_name: str, status: CallStatus, **kw) -> Call:
    return Call(
        id=str(uuid.uuid4()),
        llm_name=llm_name,
        operation=None,
        trace_id=None,
        status=status,
        **kw,
    )


def _opt_tel(
    opt: Optimizer,
    telemetry: object,
    pool: LLMPool,
    offline_calls: list[str],
) -> OptimizerTelemetry:
    return OptimizerTelemetry(opt, telemetry, pool, on_go_offline=offline_calls.append)


# ---------------------------------------------------------------------------
# FSM correctness — no stuck states
# ---------------------------------------------------------------------------


async def test_rate_limit_drives_fsm_to_offline(any_telemetry):
    """Recording max_fail_count RATE_LIMITED calls via any backend drives LLM to OFFLINE."""
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg("llm1"), "key")
    opt = Optimizer(max_fail_count=3)
    offline_calls: list[str] = []
    opt_tel = _opt_tel(opt, any_telemetry, pool, offline_calls)

    for _ in range(3):
        await opt_tel.record(_call("llm1", CallStatus.RATE_LIMITED))

    assert pool.state("llm1").phase is LifecyclePhase.OFFLINE
    assert offline_calls == ["llm1"]


async def test_probing_success_restores_available(any_telemetry):
    """OFFLINE → PROBING → OK call returns LLM to AVAILABLE with reset counters.

    This is the primary no-stuck-state test: verifies the FSM does not freeze
    at OFFLINE after a successful probe regardless of backend.
    """
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg("llm1"), "key")
    opt = Optimizer(max_fail_count=3, initial_delay=60.0)
    opt_tel = _opt_tel(opt, any_telemetry, pool, [])

    for _ in range(3):
        await opt_tel.record(_call("llm1", CallStatus.RATE_LIMITED))
    assert pool.state("llm1").phase is LifecyclePhase.OFFLINE

    pool.set_probing("llm1")
    opt.on_probing_start("llm1")

    await opt_tel.record(_call("llm1", CallStatus.OK))

    assert pool.state("llm1").phase is LifecyclePhase.AVAILABLE
    assert opt.delay_for("llm1") == pytest.approx(60.0)
    assert opt.rl_fail_count("llm1") == 0
    assert opt.probe_cycles("llm1") == 0


async def test_repeated_ok_calls_keep_available_and_converge_delay(any_telemetry):
    """Sustained OK calls keep the LLM AVAILABLE and decrease adaptive delay to its floor."""
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg("llm1"), "key")
    opt = Optimizer(initial_delay=60.0, decrease_factor=0.75)
    opt._current_delay["llm1"] = 480.0
    opt_tel = _opt_tel(opt, any_telemetry, pool, [])

    for _ in range(15):
        await opt_tel.record(_call("llm1", CallStatus.OK))

    assert pool.state("llm1").phase is LifecyclePhase.AVAILABLE
    assert opt.delay_for("llm1") == pytest.approx(60.0)
    assert opt.rl_fail_count("llm1") == 0


async def test_probing_rate_limit_immediately_re_offlines(any_telemetry):
    """A single RATE_LIMITED call while PROBING (after on_probing_start) sends LLM back OFFLINE.

    on_probing_start primes rl_fail_count to max_fail_count - 1, so one more
    failure immediately crosses the threshold — no half-stuck PROBING state.
    """
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg("llm1"), "key")
    opt = Optimizer(max_fail_count=3)
    offline_calls: list[str] = []
    opt_tel = _opt_tel(opt, any_telemetry, pool, offline_calls)

    pool.set_probing("llm1")
    opt.on_probing_start("llm1")

    await opt_tel.record(_call("llm1", CallStatus.RATE_LIMITED))

    assert pool.state("llm1").phase is LifecyclePhase.OFFLINE
    assert offline_calls == ["llm1"]


async def test_auth_failure_drops_llm_cleanly(any_telemetry):
    """HTTP 401 drops the LLM from pool immediately and fires an API-key alert.

    Verifies there is no orphaned phase-override for the removed LLM.
    """
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg("llm1"), "key")
    opt = Optimizer()
    opt_tel = _opt_tel(opt, any_telemetry, pool, [])

    await opt_tel.record(_call("llm1", CallStatus.ERROR, http_status=401))

    assert "llm1" not in pool
    alerts = opt.alerts()
    assert len(alerts) == 1
    assert "API key" in alerts[0].message
    assert "401" in alerts[0].message
    # Re-adding the name must start fresh — no stale phase override
    pool.add(_cfg("llm1"), "key")
    assert pool.state("llm1").phase is LifecyclePhase.AVAILABLE


async def test_probe_cycles_exhaust_retires_llm(any_telemetry):
    """Exhausting max_probe_cycles removes the LLM permanently and fires a retirement alert."""
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg("llm1"), "key")
    opt = Optimizer(max_probe_cycles=2)
    offline_calls: list[str] = []
    opt_tel = _opt_tel(opt, any_telemetry, pool, offline_calls)

    for _ in range(2):
        pool.set_probing("llm1")
        await opt_tel.record(_call("llm1", CallStatus.ERROR))

    assert "llm1" not in pool
    alerts = opt.alerts()
    assert len(alerts) == 1
    assert "retired" in alerts[0].message


async def test_probe_cycles_partial_keep_llm_in_pool(any_telemetry):
    """Fewer than max_probe_cycles failures keep the LLM in the pool (OFFLINE, not dropped)."""
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg("llm1"), "key")
    opt = Optimizer(max_probe_cycles=3)
    opt_tel = _opt_tel(opt, any_telemetry, pool, [])

    for _ in range(2):
        pool.set_probing("llm1")
        await opt_tel.record(_call("llm1", CallStatus.ERROR))

    assert "llm1" in pool
    assert opt.probe_cycles("llm1") == 2
    assert opt.alerts() == []


# ---------------------------------------------------------------------------
# Optimizer real usefulness
# ---------------------------------------------------------------------------


async def test_optimizer_circuit_breaks_unreliable_prefers_stable(any_telemetry):
    """Full optimizer loop: unreliable LLM goes OFFLINE; healthy LLM stays AVAILABLE.

    Also verifies that after accumulating samples the policy consistently routes
    to the stable LLM, demonstrating the real benefit of the optimizer over
    blind round-robin or first-available selection.
    """
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg("stable"), "key")
    pool.add(_cfg("flaky"), "key")
    opt = Optimizer(
        max_fail_count=3,
        min_sample_count=5,
        exploration_fraction=0.0,
        usable_rate_floor=0.0,
    )
    offline_calls: list[str] = []
    opt_tel = _opt_tel(opt, any_telemetry, pool, offline_calls)

    for _ in range(5):
        await opt_tel.record(_call("stable", CallStatus.OK, latency_ms=50))

    for _ in range(3):
        await opt_tel.record(_call("flaky", CallStatus.RATE_LIMITED))

    assert pool.state("flaky").phase is LifecyclePhase.OFFLINE
    assert pool.state("stable").phase is LifecyclePhase.AVAILABLE
    assert offline_calls == ["flaky"]

    policy = OptimizerPolicy(opt)
    selected = {
        policy.select([_cfg("stable"), _cfg("flaky")], operation=None).name for _ in range(20)
    }
    assert "stable" in selected
    assert "flaky" not in selected, "'flaky' should be below quality floor"


async def test_policy_prefers_lower_latency_when_quality_equal(any_telemetry):
    """OptimizerPolicy picks the faster LLM when success rates are equal."""
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg("fast"), "key")
    pool.add(_cfg("slow"), "key")
    opt = Optimizer(min_sample_count=10, exploration_fraction=0.0, usable_rate_floor=0.0)
    opt_tel = _opt_tel(opt, any_telemetry, pool, [])

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
    opt_tel = _opt_tel(opt, any_telemetry, pool, [])

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
# Warm-start via seed_from_metrics (queryable backends only)
# ---------------------------------------------------------------------------


async def test_seed_from_metrics_primes_max_delay_for_failed_llm(queryable_telemetry):
    """Persisted RATE_LIMITED call causes seed_from_metrics to prime max_delay on restart."""
    await queryable_telemetry.record(_call("flaky-llm", CallStatus.RATE_LIMITED))

    opt = Optimizer(initial_delay=60.0, max_delay=3600.0)
    metrics = await queryable_telemetry.metrics()
    opt.seed_from_metrics(metrics)

    assert opt.delay_for("flaky-llm") == pytest.approx(3600.0)


async def test_seed_from_metrics_leaves_healthy_llm_at_initial_delay(queryable_telemetry):
    """Persisted OK calls do not prime max_delay — delay stays at initial_delay."""
    await queryable_telemetry.record(_call("healthy-llm", CallStatus.OK))

    opt = Optimizer(initial_delay=60.0, max_delay=3600.0)
    metrics = await queryable_telemetry.metrics()
    opt.seed_from_metrics(metrics)

    assert opt.delay_for("healthy-llm") == pytest.approx(60.0)


async def test_seed_from_metrics_mixed_history(queryable_telemetry):
    """Only LLMs with last_status=RATE_LIMITED/UNAVAILABLE get max_delay primed."""
    await queryable_telemetry.record(_call("llm-ok", CallStatus.OK))
    await queryable_telemetry.record(_call("llm-rl", CallStatus.RATE_LIMITED))
    await queryable_telemetry.record(_call("llm-unavail", CallStatus.UNAVAILABLE))

    opt = Optimizer(initial_delay=60.0, max_delay=3600.0)
    metrics = await queryable_telemetry.metrics()
    opt.seed_from_metrics(metrics)

    assert opt.delay_for("llm-ok") == pytest.approx(60.0)
    assert opt.delay_for("llm-rl") == pytest.approx(3600.0)
    assert opt.delay_for("llm-unavail") == pytest.approx(3600.0)

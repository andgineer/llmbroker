"""Tests for Optimizer adaptive delay, FSM transitions, and OptimizerTelemetry."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx

from llmbroker.broker import AsyncBroker
from llmbroker.broker.pool import LLMPool
from llmbroker.models import (
    Call,
    CallStatus,
    LifecyclePhase,
    LLMConfig,
    LLMMetrics,
)
from llmbroker.optimizer import Optimizer, OptimizerTelemetry
from llmbroker.protocols.telemetry import QueryableTelemetryProtocol
from llmbroker.standalone.registry import Registry
from llmbroker.standalone.telemetry import NoTelemetry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(name: str = "x") -> LLMConfig:
    return LLMConfig(name=name, base_url="https://x/v1", model="m", api_key_ref="k")


def _call(name: str, status: CallStatus, operation: str | None = None) -> Call:
    return Call(
        id="test",
        llm_name=name,
        operation=operation,
        trace_id=None,
        status=status,
    )


def _registry(tmp_path, name="p1"):
    f = tmp_path / "llms.toml"
    f.write_text(f'[[llms]]\nname="{name}"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n')
    return Registry(f)


# ---------------------------------------------------------------------------
# Optimizer unit tests
# ---------------------------------------------------------------------------


def test_delay_increases_on_rate_limit():
    opt = Optimizer(initial_delay=60.0, max_delay=3600.0, backoff_factor=2.0)
    d1 = opt.on_rate_limited("x")
    assert d1 == 120.0
    d2 = opt.on_rate_limited("x")
    assert d2 == 240.0


def test_delay_caps_at_max():
    opt = Optimizer(initial_delay=60.0, max_delay=100.0, backoff_factor=2.0)
    for _ in range(10):
        opt.on_rate_limited("x")
    assert opt.delay_for("x") == 100.0


def test_delay_decreases_on_success():
    opt = Optimizer(initial_delay=60.0, decrease_factor=0.5)
    opt._current_delay["x"] = 480.0
    opt.on_success("x")
    assert opt.delay_for("x") == 240.0
    opt.on_success("x")
    assert opt.delay_for("x") == 120.0


def test_delay_does_not_drop_below_initial_on_success():
    opt = Optimizer(initial_delay=60.0, decrease_factor=0.5)
    opt._current_delay["x"] = 70.0
    opt.on_success("x")
    assert opt.delay_for("x") == 60.0


def test_probing_success_resets_delay():
    opt = Optimizer(initial_delay=60.0)
    opt._current_delay["x"] = 3600.0
    opt._rl_fail_count["x"] = 5
    opt.on_probing_success("x")
    assert opt.delay_for("x") == 60.0
    assert opt.rl_fail_count("x") == 0


def test_seed_from_metrics_conservative():
    opt = Optimizer(initial_delay=60.0, max_delay=3600.0)
    metrics = {
        "x": LLMMetrics(call_count=5, last_status=CallStatus.RATE_LIMITED, last_at=None),
        "y": LLMMetrics(call_count=3, last_status=CallStatus.OK, last_at=None),
        "z": LLMMetrics(call_count=1, last_status=CallStatus.UNAVAILABLE, last_at=None),
    }
    opt.seed_from_metrics(metrics)
    assert opt.delay_for("x") == 3600.0
    assert opt.delay_for("y") == 60.0  # unchanged — stays at initial
    assert opt.delay_for("z") == 3600.0


def test_on_probing_start_primes_fail_count():
    opt = Optimizer(max_fail_count=3)
    opt.on_probing_start("x")
    assert opt.rl_fail_count("x") == 2  # max_fail_count - 1


def test_success_resets_rl_fail_count():
    opt = Optimizer()
    opt._rl_fail_count["x"] = 2
    opt.on_success("x")
    assert opt.rl_fail_count("x") == 0


def test_alerts_consume_and_clear():
    opt = Optimizer()
    opt.add_alert("boom")
    opt.add_alert("bam")
    first = opt.alerts()
    assert [a.message for a in first] == ["boom", "bam"]
    second = opt.alerts()
    assert second == []


# ---------------------------------------------------------------------------
# FSM transition tests (via OptimizerTelemetry)
# ---------------------------------------------------------------------------


def _make_opt_tel(opt: Optimizer, pool: LLMPool, offline_calls: list):
    return OptimizerTelemetry(
        opt,
        NoTelemetry(),
        pool,
        on_go_offline=offline_calls.append,
    )


def test_fsm_available_to_cooling():
    """One RATE_LIMITED record: optimizer increments fail count but pool stays AVAILABLE/COOLING."""

    async def run():
        pool = LLMPool(state_store=None, user_id=None)
        cfg = _cfg()
        pool.add(cfg, "key")
        opt = Optimizer(max_fail_count=3)
        offline_calls: list[str] = []
        opt_tel = _make_opt_tel(opt, pool, offline_calls)

        await opt_tel.record(_call("x", CallStatus.RATE_LIMITED))

        assert opt.rl_fail_count("x") == 1
        assert pool.state("x").phase is not LifecyclePhase.OFFLINE
        assert offline_calls == []

    asyncio.run(run())


def test_fsm_cooling_to_offline():
    """Enough RATE_LIMITED records trigger OFFLINE via set_offline."""

    async def run():
        pool = LLMPool(state_store=None, user_id=None)
        cfg = _cfg()
        pool.add(cfg, "key")
        opt = Optimizer(max_fail_count=3)
        offline_calls: list[str] = []
        opt_tel = _make_opt_tel(opt, pool, offline_calls)

        for _ in range(3):
            await opt_tel.record(_call("x", CallStatus.RATE_LIMITED))

        assert pool.state("x").phase is LifecyclePhase.OFFLINE
        assert offline_calls == ["x"]

    asyncio.run(run())


def test_fsm_probing_to_available():
    """OK record while phase is PROBING restores AVAILABLE."""

    async def run():
        pool = LLMPool(state_store=None, user_id=None)
        cfg = _cfg()
        pool.add(cfg, "key")
        pool.set_probing("x")
        opt = Optimizer()
        opt_tel = _make_opt_tel(opt, pool, [])

        await opt_tel.record(_call("x", CallStatus.OK))

        assert pool.state("x").phase is LifecyclePhase.AVAILABLE

    asyncio.run(run())


def test_fsm_probing_failure_to_offline():
    """One RATE_LIMITED while PROBING (after on_probing_start) immediately re-triggers OFFLINE."""

    async def run():
        pool = LLMPool(state_store=None, user_id=None)
        cfg = _cfg()
        pool.add(cfg, "key")
        pool.set_probing("x")
        opt = Optimizer(max_fail_count=3)
        opt.on_probing_start("x")  # primes rl_fail_count to max_fail_count - 1
        offline_calls: list[str] = []
        opt_tel = _make_opt_tel(opt, pool, offline_calls)

        await opt_tel.record(_call("x", CallStatus.RATE_LIMITED))

        assert pool.state("x").phase is LifecyclePhase.OFFLINE
        assert offline_calls == ["x"]

    asyncio.run(run())


def test_fsm_offline_generates_alert():
    async def run():
        pool = LLMPool(state_store=None, user_id=None)
        pool.add(_cfg(), "key")
        opt = Optimizer(max_fail_count=2)
        opt_tel = _make_opt_tel(opt, pool, [])

        for _ in range(2):
            await opt_tel.record(_call("x", CallStatus.RATE_LIMITED))

        alerts = opt.alerts()
        assert len(alerts) == 1
        assert "x" in alerts[0].message
        assert "OFFLINE" in alerts[0].message

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Broker-level integration tests
# ---------------------------------------------------------------------------


def test_cold_boot_no_telemetry(tmp_path):
    """AsyncBroker with NoTelemetry and optimize=True provisions without error."""

    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path),
            telemetry=NoTelemetry(),
            optimize=True,
        ) as broker:
            assert await broker.count() == 1
            assert await broker.alerts() == []

    asyncio.run(run())


class _FakeQueryableTelemetry:
    """Minimal queryable telemetry stub returning preset metrics."""

    def __init__(self, metrics: dict[str, LLMMetrics]) -> None:
        self._metrics = metrics

    async def record(self, call: Call) -> None:
        pass

    async def record_quality(self, call_id: str, score: float) -> None:
        pass

    async def metrics(
        self, *, since: datetime | None = None, user_id: object = None
    ) -> dict[str, LLMMetrics]:
        return self._metrics

    async def calls(self, *, limit: int, user_id: object = None) -> list[Call]:
        return []

    async def purge_calls(self, *, before: datetime) -> int:
        return 0


def test_warm_start_activates_with_queryable(tmp_path):
    """Queryable backend causes seed_from_metrics to prime max_delay for failed LLMs."""
    metrics = {"p1": LLMMetrics(call_count=3, last_status=CallStatus.RATE_LIMITED, last_at=None)}
    tel = _FakeQueryableTelemetry(metrics)

    async def run():
        opt = Optimizer()
        async with AsyncBroker(
            registry=_registry(tmp_path),
            telemetry=tel,
            optimize=opt,
        ):
            assert opt.delay_for("p1") == opt.max_delay

    asyncio.run(run())


def test_alerts_returns_offline_llm(tmp_path):
    """OFFLINE transition produces an alert retrievable via broker.alerts()."""

    async def run():
        opt = Optimizer(max_fail_count=2)
        pool = LLMPool(state_store=None, user_id=None)
        pool.add(_cfg("p1"), "key")
        offline_calls: list[str] = []
        opt_tel = OptimizerTelemetry(opt, NoTelemetry(), pool, on_go_offline=offline_calls.append)

        for _ in range(2):
            await opt_tel.record(_call("p1", CallStatus.RATE_LIMITED))

        alerts = opt.alerts()
        assert len(alerts) == 1
        assert "p1" in alerts[0].message

    asyncio.run(run())


def test_optimizer_telemetry_proxies_queryable():
    """isinstance check passes when inner backend is queryable."""
    tel = _FakeQueryableTelemetry({})
    pool = LLMPool(state_store=None, user_id=None)
    opt = Optimizer()
    opt_tel = OptimizerTelemetry(opt, tel, pool, on_go_offline=lambda _: None)
    assert isinstance(opt_tel, QueryableTelemetryProtocol)


# ---------------------------------------------------------------------------
# Pool phase override tests
# ---------------------------------------------------------------------------


def test_pool_set_offline_returns_offline_phase():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), "k")
    pool.set_offline("x")
    assert pool.state("x").phase is LifecyclePhase.OFFLINE


def test_pool_set_probing_returns_probing_phase():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), "k")
    pool.set_probing("x")
    assert pool.state("x").phase is LifecyclePhase.PROBING


def test_pool_set_available_clears_offline():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), "k")
    pool.set_offline("x")
    pool.set_available("x")
    assert pool.state("x").phase is LifecyclePhase.AVAILABLE


def test_pool_set_available_clears_cooldown():
    """set_available must clear both phase override AND cooldown so state returns AVAILABLE."""
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), "k")
    # Manually set a future cooldown to simulate an in-progress cool_down timer
    future = datetime.now(UTC) + timedelta(seconds=300)
    pool._state.set_cooling("x", future, 1)
    pool.set_offline("x")
    pool.set_available("x")
    assert pool.state("x").phase is LifecyclePhase.AVAILABLE


def test_reenqueue_skips_when_offline():
    """cool_down timer callback does not re-add slot when LLM is OFFLINE."""

    async def run():
        pool = LLMPool(state_store=None, user_id=None)
        cfg = _cfg()
        pool.add(cfg, "k")

        captured_callback = None

        class _CapturingLoop:
            def call_later(self, delay, fn, *args):
                nonlocal captured_callback
                captured_callback = lambda: fn(*args)

        with patch("llmbroker.broker.pool.asyncio.get_running_loop", return_value=_CapturingLoop()):
            await pool.cool_down(cfg, httpx.Headers({"retry-after": "30"}))

        pool.set_offline("x")
        size_before = pool._queue.qsize()
        captured_callback()
        assert pool._queue.qsize() == size_before  # slot NOT re-added

    asyncio.run(run())


def test_fsm_probing_error_returns_to_offline():
    """ERROR record while PROBING re-triggers OFFLINE and on_go_offline."""

    async def run():
        pool = LLMPool(state_store=None, user_id=None)
        pool.add(_cfg(), "key")
        pool.set_probing("x")
        opt = Optimizer()
        offline_calls: list[str] = []
        opt_tel = _make_opt_tel(opt, pool, offline_calls)

        await opt_tel.record(_call("x", CallStatus.ERROR))

        assert pool.state("x").phase is LifecyclePhase.OFFLINE
        assert offline_calls == ["x"]

    asyncio.run(run())


def test_pool_drop_clears_phase_override():
    """drop clears the phase override so a re-added LLM starts AVAILABLE."""
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), "k")
    pool.set_offline("x")
    pool.drop("x")
    pool.add(_cfg(), "k")
    assert pool.state("x").phase is LifecyclePhase.AVAILABLE

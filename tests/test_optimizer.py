"""Tests for Optimizer failure bookkeeping, retirement, and OptimizerTelemetry."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from llmbroker.broker import AsyncBroker
from llmbroker.broker.pool import LLMPool
from llmbroker.broker.router import Router
from llmbroker.exceptions import NoLLMAvailableError
from llmbroker.models import (
    Call,
    CallStatus,
    LLMConfig,
    LLMMetrics,
    Usage,
)
from llmbroker.optimizer import FirstAvailablePolicy, Optimizer, OptimizerPolicy, OptimizerTelemetry
from llmbroker.protocols.telemetry import QueryableTelemetryProtocol
from llmbroker.standalone.registry import Registry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.telemetry import NoTelemetry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(name: str = "x") -> LLMConfig:
    return LLMConfig(name=name, base_url="https://x/v1", model="m", api_key_ref="k")


def _call(
    name: str,
    status: CallStatus,
    operation: str | None = None,
    latency_ms: int | None = None,
    usage: Usage | None = None,
    http_status: int | None = None,
) -> Call:
    return Call(
        id="test",
        llm_name=name,
        operation=operation,
        trace_id=None,
        status=status,
        latency_ms=latency_ms,
        usage=usage,
        http_status=http_status,
    )


def _registry(tmp_path, name="p1"):
    f = tmp_path / "llms.toml"
    f.write_text(f'[[llms]]\nname="{name}"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n')
    return Registry(f)


def _opt_tel(opt: Optimizer, pool: LLMPool, telemetry=None) -> OptimizerTelemetry:
    return OptimizerTelemetry(opt, telemetry or NoTelemetry(), pool)


# ---------------------------------------------------------------------------
# Optimizer unit tests
# ---------------------------------------------------------------------------


def test_rl_fail_count_increments_on_rate_limit():
    opt = Optimizer()
    opt.on_rate_limited("x")
    opt.on_rate_limited("x")
    assert opt.rl_fail_count("x") == 2


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
# should_retire / automatic retirement
# ---------------------------------------------------------------------------


def test_should_retire_below_removal_floor_with_enough_samples():
    opt = Optimizer(min_sample_count=5, removal_rate_floor=0.5)
    for _ in range(10):
        opt._record_rolling("x", None, _call("x", CallStatus.ERROR))
    assert opt.should_retire("x", None) is True


def test_should_not_retire_without_enough_samples():
    opt = Optimizer(min_sample_count=10, removal_rate_floor=0.9)
    for _ in range(3):
        opt._record_rolling("x", None, _call("x", CallStatus.ERROR))
    assert opt.should_retire("x", None) is False


def test_between_removal_floor_and_usable_floor_not_retired():
    """Distinguishes the two thresholds: below usable_rate_floor but above removal_rate_floor."""
    opt = Optimizer(min_sample_count=10, usable_rate_floor=0.6, removal_rate_floor=0.1)
    # 3/10 OK -> Laplace rate (3+1)/(10+2) = 0.333: below usable floor, above removal floor.
    for _ in range(3):
        opt._record_rolling("x", None, _call("x", CallStatus.OK))
    for _ in range(7):
        opt._record_rolling("x", None, _call("x", CallStatus.ERROR))
    assert opt.usable_rate("x", None) < opt.usable_rate_floor
    assert opt.should_retire("x", None) is False


def test_well_behaved_daily_capped_llm_not_flagged_for_removal():
    """A long, honored cooldown produces no failed attempts, so usable_rate stays high."""
    opt = Optimizer(min_sample_count=5, removal_rate_floor=0.5)
    for _ in range(20):
        opt._record_rolling("x", None, _call("x", CallStatus.OK))
    assert opt.should_retire("x", None) is False


def test_auth_failure_401_drops_llm_and_alerts():
    async def run():
        pool = LLMPool(state_store=None, user_id=None)
        pool.add(_cfg(), "key")
        opt = Optimizer()
        opt_tel = _opt_tel(opt, pool)

        await opt_tel.record(_call("x", CallStatus.ERROR, http_status=401))

        assert "x" not in pool
        alerts = opt.alerts()
        assert len(alerts) == 1
        assert "API key" in alerts[0].message
        assert "401" in alerts[0].message
        assert "'k'" in alerts[0].message  # api_key_ref value

    asyncio.run(run())


def test_auth_failure_403_drops_llm_and_alerts():
    async def run():
        pool = LLMPool(state_store=None, user_id=None)
        pool.add(_cfg(), "key")
        opt = Optimizer()
        opt_tel = _opt_tel(opt, pool)

        await opt_tel.record(_call("x", CallStatus.ERROR, http_status=403))

        assert "x" not in pool
        alerts = opt.alerts()
        assert len(alerts) == 1
        assert "403" in alerts[0].message

    asyncio.run(run())


def test_generic_error_below_removal_floor_retires_with_alert():
    async def run():
        pool = LLMPool(state_store=None, user_id=None)
        pool.add(_cfg(), "key")
        opt = Optimizer(min_sample_count=3, removal_rate_floor=0.9)
        opt_tel = _opt_tel(opt, pool)

        for _ in range(3):
            await opt_tel.record(_call("x", CallStatus.ERROR))

        assert "x" not in pool
        alerts = opt.alerts()
        assert len(alerts) == 1
        assert "retired" in alerts[0].message

    asyncio.run(run())


def test_rate_limit_below_removal_floor_retires_with_alert():
    async def run():
        pool = LLMPool(state_store=None, user_id=None)
        pool.add(_cfg(), "key")
        opt = Optimizer(min_sample_count=3, removal_rate_floor=0.9)
        opt_tel = _opt_tel(opt, pool)

        for _ in range(3):
            await opt_tel.record(_call("x", CallStatus.RATE_LIMITED))

        assert "x" not in pool
        alerts = opt.alerts()
        assert len(alerts) == 1
        assert "retired" in alerts[0].message

    asyncio.run(run())


def test_ok_calls_do_not_retire():
    async def run():
        pool = LLMPool(state_store=None, user_id=None)
        pool.add(_cfg(), "key")
        opt = Optimizer(min_sample_count=3, removal_rate_floor=0.9)
        opt_tel = _opt_tel(opt, pool)

        for _ in range(5):
            await opt_tel.record(_call("x", CallStatus.OK))

        assert "x" in pool
        assert opt.alerts() == []

    asyncio.run(run())


def test_drop_removes_config_but_not_queue_slot():
    """drop() removes from _configs but leaves the stale slot in the queue.

    This verifies the pool-side precondition for the router guard
    (`if config.name not in self._pool: continue`). The guard itself is
    pre-existing code exercised in router integration tests, not here.
    """

    async def run():
        pool = LLMPool(state_store=None, user_id=None)
        pool.add(_cfg(), "key")
        pool.drop("x")
        stale = await pool.acquire(0)  # stale slot still drainable from queue
        assert stale.name == "x"
        assert "x" not in pool  # _configs was cleared by drop

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


def test_optimizer_telemetry_proxies_queryable():
    """isinstance check passes when inner backend is queryable."""
    tel = _FakeQueryableTelemetry({})
    pool = LLMPool(state_store=None, user_id=None)
    opt = Optimizer()
    opt_tel = OptimizerTelemetry(opt, tel, pool)
    assert isinstance(opt_tel, QueryableTelemetryProtocol)


# ---------------------------------------------------------------------------
# Rolling-aggregate and stats tests
# ---------------------------------------------------------------------------


def test_record_rolling_populates_deque():
    opt = Optimizer(rolling_window=3)
    for i in range(4):
        opt._record_rolling("x", "op", _call("x", CallStatus.OK))
    deque = opt._rolling[("x", "op")]
    assert len(deque) == 3  # maxlen=3 enforced


def test_usable_rate_none_below_min_sample_count():
    opt = Optimizer(min_sample_count=10)
    for _ in range(9):
        opt._record_rolling("x", None, _call("x", CallStatus.OK))
    assert opt.usable_rate("x", None) is None


def test_usable_rate_laplace_smoothed():
    opt = Optimizer(min_sample_count=10)
    for _ in range(5):
        opt._record_rolling("x", None, _call("x", CallStatus.OK))
    for _ in range(5):
        opt._record_rolling("x", None, _call("x", CallStatus.ERROR))
    rate = opt.usable_rate("x", None)
    assert rate is not None
    assert abs(rate - (5 + 1) / (10 + 2)) < 1e-9


def test_mean_latency_ignores_non_ok():
    opt = Optimizer(min_sample_count=1)
    opt._record_rolling("x", None, _call("x", CallStatus.ERROR, latency_ms=9999))
    opt._record_rolling("x", None, _call("x", CallStatus.OK, latency_ms=100))
    opt._record_rolling("x", None, _call("x", CallStatus.OK, latency_ms=200))
    assert opt.mean_latency_ms("x", None) == 150.0


# ---------------------------------------------------------------------------
# SelectionPolicy tests
# ---------------------------------------------------------------------------


def test_first_available_policy_picks_first():
    policy = FirstAvailablePolicy()
    a, b, c = _cfg("a"), _cfg("b"), _cfg("c")
    assert policy.select([a, b, c], operation=None) is a


def test_first_available_empty_returns_none():
    assert FirstAvailablePolicy().select([], operation=None) is None


def _opt_with_samples(
    llm_name: str,
    ok: int,
    err: int,
    latency_ms: int,
    operation: str | None,
    min_sample_count: int = 5,
) -> Optimizer:
    opt = Optimizer(min_sample_count=min_sample_count, rolling_window=100)
    for _ in range(ok):
        opt._record_rolling(
            llm_name, operation, _call(llm_name, CallStatus.OK, latency_ms=latency_ms)
        )
    for _ in range(err):
        opt._record_rolling(llm_name, operation, _call(llm_name, CallStatus.ERROR))
    return opt


def test_optimizer_policy_no_data_does_not_filter():
    opt = Optimizer(min_sample_count=10, exploration_fraction=0.0)
    a, b = _cfg("a"), _cfg("b")
    policy = OptimizerPolicy(opt)
    result = policy.select([a, b], operation=None)
    assert result in (a, b)


def test_optimizer_policy_quality_floor_gates():
    opt = Optimizer(min_sample_count=5, usable_rate_floor=0.6, exploration_fraction=0.0)
    # "a" has a low usable rate; "b" has high rate
    for _ in range(5):
        opt._record_rolling("a", "op", _call("a", CallStatus.ERROR))
    for _ in range(5):
        opt._record_rolling("b", "op", _call("b", CallStatus.OK, latency_ms=50))
    policy = OptimizerPolicy(opt)
    result = policy.select([_cfg("a"), _cfg("b")], operation="op")
    assert result is not None
    assert result.name == "b"


def test_optimizer_policy_quality_floor_fallback_when_all_fail():
    # All candidates fail the floor — must not raise, must return something
    opt = Optimizer(min_sample_count=5, usable_rate_floor=0.99, exploration_fraction=0.0)
    for name in ("a", "b"):
        for _ in range(5):
            opt._record_rolling(name, None, _call(name, CallStatus.ERROR))
    policy = OptimizerPolicy(opt)
    result = policy.select([_cfg("a"), _cfg("b")], operation=None)
    assert result is not None
    alerts = opt.alerts()
    assert any("score-ranked fallback" in a.message for a in alerts)


def test_floor_alert_debounced():
    """Two consecutive selects that both fail the floor produce only one alert."""
    opt = Optimizer(min_sample_count=5, usable_rate_floor=0.99, exploration_fraction=0.0)
    for name in ("a", "b"):
        for _ in range(5):
            opt._record_rolling(name, None, _call(name, CallStatus.ERROR))
    policy = OptimizerPolicy(opt)
    policy.select([_cfg("a"), _cfg("b")], operation=None)
    policy.select([_cfg("a"), _cfg("b")], operation=None)
    assert len(opt.alerts()) == 1


def test_floor_alert_debounce_separate_operations():
    """Floor alert debounce is per-operation — different operations each get one alert."""
    opt = Optimizer(min_sample_count=5, usable_rate_floor=0.99, exploration_fraction=0.0)
    for name in ("a", "b"):
        for _ in range(5):
            opt._record_rolling(name, "op1", _call(name, CallStatus.ERROR))
            opt._record_rolling(name, "op2", _call(name, CallStatus.ERROR))
    policy = OptimizerPolicy(opt)
    policy.select([_cfg("a"), _cfg("b")], operation="op1")
    policy.select([_cfg("a"), _cfg("b")], operation="op2")
    assert len(opt.alerts()) == 2


def test_optimizer_policy_background_ranks_by_quality():
    # usable_rate_floor=0.0 so both candidates pass the floor gate; the test exercises ranking only
    opt = Optimizer(
        min_sample_count=5,
        exploration_fraction=0.0,
        usable_rate_floor=0.0,
        background_operations=frozenset({"batch"}),
    )
    # "hi": 5/5 OK → Laplace rate = 6/7 ≈ 0.857; "lo": 1/5 OK → 2/7 ≈ 0.286; same latency
    for _ in range(5):
        opt._record_rolling("hi", "batch", _call("hi", CallStatus.OK, latency_ms=100))
    for _ in range(4):
        opt._record_rolling("lo", "batch", _call("lo", CallStatus.ERROR))
    opt._record_rolling("lo", "batch", _call("lo", CallStatus.OK, latency_ms=100))
    policy = OptimizerPolicy(opt)
    result = policy.select([_cfg("lo"), _cfg("hi")], operation="batch")
    assert result is not None
    assert result.name == "hi"


def test_optimizer_policy_interactive_ranks_by_latency():
    opt = Optimizer(min_sample_count=5, exploration_fraction=0.0)
    # Both have identical good quality; "fast" has lower latency
    for _ in range(5):
        opt._record_rolling("fast", "op", _call("fast", CallStatus.OK, latency_ms=10))
    for _ in range(5):
        opt._record_rolling("slow", "op", _call("slow", CallStatus.OK, latency_ms=500))
    policy = OptimizerPolicy(opt)
    result = policy.select([_cfg("slow"), _cfg("fast")], operation="op")
    assert result is not None
    assert result.name == "fast"


def test_optimizer_policy_exploration_bypasses_ranking():
    # With exploration_fraction=1.0, select is random — run enough times to observe non-determinism
    opt = Optimizer(min_sample_count=5, exploration_fraction=1.0)
    for _ in range(5):
        opt._record_rolling("fast", "op", _call("fast", CallStatus.OK, latency_ms=1))
    for _ in range(5):
        opt._record_rolling("slow", "op", _call("slow", CallStatus.OK, latency_ms=9999))
    policy = OptimizerPolicy(opt)
    candidates = [_cfg("fast"), _cfg("slow")]
    seen = {policy.select(candidates, operation="op").name for _ in range(50)}
    assert "slow" in seen


def test_optimizer_policy_unknown_latency_ranked_last_interactive():
    opt = Optimizer(min_sample_count=5, exploration_fraction=0.0)
    # "known" has measured latency; "unknown" has no OK calls → latency=inf
    for _ in range(5):
        opt._record_rolling("known", "op", _call("known", CallStatus.OK, latency_ms=999))
    policy = OptimizerPolicy(opt)
    result = policy.select([_cfg("unknown"), _cfg("known")], operation="op")
    assert result is not None
    assert result.name == "known"


# ---------------------------------------------------------------------------
# Pool drain-and-pick tests
# ---------------------------------------------------------------------------


def test_pool_acquire_drain_and_pick():
    """Multiple available slots: policy picks non-first; others returned to queue."""

    async def run():
        pool = LLMPool(state_store=None, user_id=None)
        a, b, c = _cfg("a"), _cfg("b"), _cfg("c")
        pool.add(a, "k")
        pool.add(b, "k")
        pool.add(c, "k")

        policy = MagicMock()
        policy.select.return_value = b

        result = await pool.acquire(0, policy=policy, operation="op")
        assert result is b
        assert pool._queue.qsize() == 2
        remaining = {pool._queue.get_nowait().name, pool._queue.get_nowait().name}
        assert remaining == {"a", "c"}

    asyncio.run(run())


def test_pool_acquire_single_slot_no_drain():
    """Single slot: policy receives list of one; no put_nowait overflow."""

    async def run():
        pool = LLMPool(state_store=None, user_id=None)
        a = _cfg("a")
        pool.add(a, "k")

        policy = MagicMock()
        policy.select.return_value = a

        result = await pool.acquire(0, policy=policy, operation=None)
        assert result is a
        assert pool._queue.qsize() == 0
        policy.select.assert_called_once_with([a], operation=None)

    asyncio.run(run())


def test_pool_acquire_no_policy_returns_first():
    """Without policy, acquire returns the first slot without drain."""

    async def run():
        pool = LLMPool(state_store=None, user_id=None)
        a, b = _cfg("a"), _cfg("b")
        pool.add(a, "k")
        pool.add(b, "k")

        result = await pool.acquire(0)
        assert result in (a, b)
        assert pool._queue.qsize() == 1

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Router and Broker wiring tests
# ---------------------------------------------------------------------------


def test_router_passes_policy_to_acquire():
    """Router passes its policy and operation through to pool.acquire."""

    async def run():
        pool = MagicMock()
        acquired = _cfg("x")

        async def fake_acquire(wait, *, policy, operation):
            assert policy is sentinel_policy
            assert operation == "myop"
            return acquired

        pool.acquire = fake_acquire
        pool.__contains__ = MagicMock(return_value=True)
        pool.has_key = MagicMock(return_value=True)
        pool.configs = {"x": acquired}

        async def _no_shared_cooling(_cfg):
            return False

        pool.apply_shared_cooling = _no_shared_cooling

        async def fake_call_provider(*a, **kw):
            return "text", [], None

        sentinel_policy = object()

        router = Router(pool, NoTelemetry(), user_id=None, policy=sentinel_policy)
        with patch("llmbroker.broker.router.call_provider", fake_call_provider):
            pool.release = MagicMock()
            pool.clear_cooling = MagicMock()
            result = await router.chat([{"role": "user", "content": "hi"}], operation="myop")
        assert result._llm_name == "x"

    asyncio.run(run())


def test_broker_creates_optimizer_policy_when_optimizer_set(tmp_path):
    """AsyncBroker with optimizer wires OptimizerPolicy to the Router."""

    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path),
            telemetry=NoTelemetry(),
            optimize=True,
        ) as broker:
            assert isinstance(broker._router._policy, OptimizerPolicy)

    asyncio.run(run())


def test_broker_no_policy_when_no_optimizer(tmp_path):
    """AsyncBroker with optimize=False passes policy=None; acquire never drains."""

    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path),
            telemetry=NoTelemetry(),
            optimize=False,
        ) as broker:
            assert broker._router._policy is None

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Underprovisioned alert tests
# ---------------------------------------------------------------------------


def _make_unavailable(broker, name: str) -> None:
    """Simulate a cooling LLM without going through a real cool_down cycle."""
    future = datetime.now(UTC) + timedelta(seconds=300)
    broker._pool._state.set_cooling(name, future, 1)


def test_underprovisioned_alert_when_all_cooling(tmp_path):
    """_maybe_alert_underprov fires when all keyed pool members are not AVAILABLE."""

    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path),
            secrets=DictSecrets({"K": "test"}),
            telemetry=NoTelemetry(),
            optimize=True,
        ) as broker:
            _make_unavailable(broker, "p1")
            broker._maybe_alert_underprov()
            alerts = await broker.alerts()
            assert len(alerts) == 1
            assert "under-provisioned" in alerts[0].message

    asyncio.run(run())


def test_underprov_alert_fires_despite_keyless_config_present(tmp_path):
    """Regression for the has_key filter: a keyless config (always AVAILABLE by default)
    must not mask the alarm when every *keyed* config is COOLING."""

    async def run():
        f = tmp_path / "llms.toml"
        f.write_text(
            '[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n'
            '[[llms]]\nname="p2"\nbase_url="https://y/v1"\nmodel="m"\napi_key_ref="UNRESOLVED"\n',
        )
        async with AsyncBroker(
            registry=Registry(f),
            secrets=DictSecrets({"K": "test"}),  # p2's ref is left unresolved — stays keyless
            telemetry=NoTelemetry(),
            optimize=True,
        ) as broker:
            assert not broker._pool.has_key("p2")
            _make_unavailable(broker, "p1")
            broker._maybe_alert_underprov()
            alerts = await broker.alerts()
            assert len(alerts) == 1
            assert "under-provisioned" in alerts[0].message

    asyncio.run(run())


def test_no_underprov_alert_when_some_available(tmp_path):
    """No alert when at least one keyed LLM is AVAILABLE."""

    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path, name="p1"),
            secrets=DictSecrets({"K": "test"}),
            telemetry=NoTelemetry(),
            optimize=True,
        ) as broker:
            # p1 stays AVAILABLE (default)
            broker._maybe_alert_underprov()
            alerts = await broker.alerts()
            assert alerts == []

    asyncio.run(run())


def test_no_underprov_alert_when_optimize_false(tmp_path):
    """No alert when optimize=False (no optimizer attached)."""

    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path),
            telemetry=NoTelemetry(),
            optimize=False,
        ) as broker:
            _make_unavailable(broker, "p1")
            broker._maybe_alert_underprov()
            alerts = await broker.alerts()
            assert alerts == []

    asyncio.run(run())


def test_underprov_alert_debounced(tmp_path):
    """Two consecutive calls within the debounce interval produce only one alert."""

    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path),
            secrets=DictSecrets({"K": "test"}),
            telemetry=NoTelemetry(),
            optimize=True,
        ) as broker:
            _make_unavailable(broker, "p1")
            broker._maybe_alert_underprov()
            broker._maybe_alert_underprov()  # within interval
            alerts = await broker.alerts()
            assert len(alerts) == 1

    asyncio.run(run())


def test_underprov_alert_via_ask_wiring(tmp_path):
    """ask() catches NoLLMAvailableError and calls _maybe_alert_underprov() via try/except."""

    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path),
            secrets=DictSecrets({"K": "test"}),
            telemetry=NoTelemetry(),
            optimize=True,
        ) as broker:
            # Drain the queue so the next acquire raises QueueEmpty → NoLLMAvailableError.
            # _make_unavailable marks p1 non-AVAILABLE so _maybe_alert_underprov fires.
            await broker._pool.acquire(0)
            _make_unavailable(broker, "p1")

            with pytest.raises(NoLLMAvailableError):
                await broker.ask("hi", wait=0)

            alerts = await broker.alerts()
            assert len(alerts) == 1
            assert "under-provisioned" in alerts[0].message

    asyncio.run(run())


def test_alerts_returns_empty_optimize_false(tmp_path):
    """Regression guard: AsyncBroker(optimize=False).alerts() always returns []."""

    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path),
            telemetry=NoTelemetry(),
            optimize=False,
        ) as broker:
            assert await broker.alerts() == []

    asyncio.run(run())

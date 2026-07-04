"""Tests for Optimizer failure bookkeeping, retirement, and OptimizerTelemetry."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

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
from llmbroker.optimizer import (
    _CALL_INDEX_CAP,
    FirstAvailablePolicy,
    Optimizer,
    OptimizerPolicy,
    OptimizerTelemetry,
)
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
        opt._record_transport("x", None, _call("x", CallStatus.ERROR))
    assert opt.should_retire("x", None) is True


def test_should_not_retire_without_enough_samples():
    opt = Optimizer(min_sample_count=10, removal_rate_floor=0.9)
    for _ in range(3):
        opt._record_transport("x", None, _call("x", CallStatus.ERROR))
    assert opt.should_retire("x", None) is False


def test_between_removal_floor_and_usable_floor_not_retired():
    """Distinguishes the two thresholds: below usable_rate_floor but above removal_rate_floor.

    Hand-computed decayed Jeffreys rate (d=9/11) for 3 OK then 7 ERROR: weight~=4.7606,
    weighted_good~=0.6106 -> rate~=0.1928: below usable floor (0.6), above removal floor (0.1).
    """
    opt = Optimizer(min_sample_count=10, usable_rate_floor=0.6, removal_rate_floor=0.1)
    for _ in range(3):
        opt._record_transport("x", None, _call("x", CallStatus.OK))
    for _ in range(7):
        opt._record_transport("x", None, _call("x", CallStatus.ERROR))
    rate = opt.usable_rate("x", None)
    assert rate is not None
    assert rate == pytest.approx(0.192785, abs=1e-5)
    assert rate < opt.usable_rate_floor
    assert opt.should_retire("x", None) is False


def test_well_behaved_daily_capped_llm_not_flagged_for_removal():
    """A long, honored cooldown produces no failed attempts, so usable_rate stays high."""
    opt = Optimizer(min_sample_count=5, removal_rate_floor=0.5)
    for _ in range(20):
        opt._record_transport("x", None, _call("x", CallStatus.OK))
    assert opt.should_retire("x", None) is False


def test_auth_failure_401_drops_llm_and_alerts():
    async def run():
        pool = LLMPool(state_store=None, user_id=None)
        await pool.add(_cfg(), "key")
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
        await pool.add(_cfg(), "key")
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
        await pool.add(_cfg(), "key")
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
        await pool.add(_cfg(), "key")
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
        await pool.add(_cfg(), "key")
        opt = Optimizer(min_sample_count=3, removal_rate_floor=0.9)
        opt_tel = _opt_tel(opt, pool)

        for _ in range(5):
            await opt_tel.record(_call("x", CallStatus.OK))

        assert "x" in pool
        assert opt.alerts() == []

    asyncio.run(run())


def test_drop_removes_the_slot_entirely():
    async def run():
        pool = LLMPool(state_store=None, user_id=None)
        await pool.add(_cfg(), "key")
        await pool.drop("x")
        assert "x" not in pool
        with pytest.raises(TimeoutError):
            await pool.acquire(0)

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
# Decayed ranking-aggregate tests (d_rank = 9/11, no window — no eviction)
# ---------------------------------------------------------------------------


def test_record_transport_count_is_not_windowed():
    """Unlike the old rolling deque, count keeps growing — there is no window to evict from."""
    opt = Optimizer()
    for _ in range(50):
        opt._record_transport("x", "op", _call("x", CallStatus.OK))
    assert opt._ranking[("x", "op")].count == 50


def test_usable_rate_none_below_min_sample_count():
    opt = Optimizer(min_sample_count=10)
    for _ in range(9):
        opt._record_transport("x", None, _call("x", CallStatus.OK))
    assert opt.usable_rate("x", None) is None


def test_usable_rate_becomes_non_none_exactly_at_min_sample_count():
    """The trust gate reads count, not the asymptotic weight."""
    opt = Optimizer(min_sample_count=10)
    for _ in range(9):
        opt._record_transport("x", None, _call("x", CallStatus.OK))
    assert opt.usable_rate("x", None) is None
    opt._record_transport("x", None, _call("x", CallStatus.OK))
    assert opt.usable_rate("x", None) is not None


def test_usable_rate_jeffreys_smoothed_decayed():
    """Hand-computed decayed Jeffreys rate (d=9/11) for 5 OK then 5 ERROR: weight~=4.7606,
    weighted_good~=1.2772 -> rate~=0.3085 (not the old Laplace (5+1)/(10+2)=0.5)."""
    opt = Optimizer(min_sample_count=10)
    for _ in range(5):
        opt._record_transport("x", None, _call("x", CallStatus.OK))
    for _ in range(5):
        opt._record_transport("x", None, _call("x", CallStatus.ERROR))
    rate = opt.usable_rate("x", None)
    assert rate is not None
    assert rate == pytest.approx(0.3085069042, abs=1e-6)


def test_usable_rate_saturated_good_crosses_floor_after_four_failures():
    """Regression: a saturated-good model's recent regression is no longer masked.

    Hand-computed: saturating at all-OK gives weight~=5.5 (ceiling 1/(1-d)), rate~=0.9231.
    Exactly 4 consecutive failures from that state drop it below 0.5 (rate~=0.4561).
    """
    opt = Optimizer(min_sample_count=1, usable_rate_floor=0.5)
    for _ in range(500):
        opt._record_transport("x", None, _call("x", CallStatus.OK))
    assert opt.usable_rate("x", None) == pytest.approx(0.923077, abs=1e-5)
    for _ in range(3):
        opt._record_transport("x", None, _call("x", CallStatus.ERROR))
    assert opt.usable_rate("x", None) >= opt.usable_rate_floor
    opt._record_transport("x", None, _call("x", CallStatus.ERROR))
    rate = opt.usable_rate("x", None)
    assert rate is not None
    assert rate < opt.usable_rate_floor
    assert rate == pytest.approx(0.456106, abs=1e-5)


def test_usable_rate_stale_bad_streak_forgiven_after_comparable_good_streak():
    """A model is not permanently punished — decay forgives an old failure streak.

    Hand-computed: 10 fails from scratch give rate~=0.0868 (retirement reachable, <0.15);
    4 subsequent OK events bring it back above the 0.5 routing floor (rate~=0.5731).
    """
    opt = Optimizer(min_sample_count=1, usable_rate_floor=0.5, removal_rate_floor=0.15)
    for _ in range(10):
        opt._record_transport("x", None, _call("x", CallStatus.ERROR))
    rate = opt.usable_rate("x", None)
    assert rate is not None
    assert rate == pytest.approx(0.086796, abs=1e-5)
    assert opt.should_retire("x", None) is True
    for _ in range(4):
        opt._record_transport("x", None, _call("x", CallStatus.OK))
    rate = opt.usable_rate("x", None)
    assert rate is not None
    assert rate >= opt.usable_rate_floor
    assert rate == pytest.approx(0.573108, abs=1e-5)


def test_mean_latency_ignores_non_ok():
    """Hand-computed decayed mean (d=9/11) for OK(100) then OK(200): weight~=1.8182,
    weighted_good~=281.8182 -> mean~=155.0 (not the plain average 150 — recency-weighted)."""
    opt = Optimizer(min_sample_count=1)
    opt._record_transport("x", None, _call("x", CallStatus.ERROR, latency_ms=9999))
    opt._record_transport("x", None, _call("x", CallStatus.OK, latency_ms=100))
    opt._record_transport("x", None, _call("x", CallStatus.OK, latency_ms=200))
    assert opt.mean_latency_ms("x", None) == pytest.approx(155.0)


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
    opt = Optimizer(min_sample_count=min_sample_count)
    for _ in range(ok):
        opt._record_transport(
            llm_name, operation, _call(llm_name, CallStatus.OK, latency_ms=latency_ms)
        )
    for _ in range(err):
        opt._record_transport(llm_name, operation, _call(llm_name, CallStatus.ERROR))
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
        opt._record_transport("a", "op", _call("a", CallStatus.ERROR))
    for _ in range(5):
        opt._record_transport("b", "op", _call("b", CallStatus.OK, latency_ms=50))
    policy = OptimizerPolicy(opt)
    result = policy.select([_cfg("a"), _cfg("b")], operation="op")
    assert result is not None
    assert result.name == "b"


def test_optimizer_policy_quality_floor_fallback_when_all_fail():
    # All candidates fail the floor — must not raise, must return something
    opt = Optimizer(min_sample_count=5, usable_rate_floor=0.99, exploration_fraction=0.0)
    for name in ("a", "b"):
        for _ in range(5):
            opt._record_transport(name, None, _call(name, CallStatus.ERROR))
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
            opt._record_transport(name, None, _call(name, CallStatus.ERROR))
    policy = OptimizerPolicy(opt)
    policy.select([_cfg("a"), _cfg("b")], operation=None)
    policy.select([_cfg("a"), _cfg("b")], operation=None)
    assert len(opt.alerts()) == 1


def test_floor_alert_debounce_separate_operations():
    """Floor alert debounce is per-operation — different operations each get one alert."""
    opt = Optimizer(min_sample_count=5, usable_rate_floor=0.99, exploration_fraction=0.0)
    for name in ("a", "b"):
        for _ in range(5):
            opt._record_transport(name, "op1", _call(name, CallStatus.ERROR))
            opt._record_transport(name, "op2", _call(name, CallStatus.ERROR))
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
    # "hi": 5/5 OK → high decayed rate; "lo": 1 OK after 4 ERROR → low decayed rate; same latency
    for _ in range(5):
        opt._record_transport("hi", "batch", _call("hi", CallStatus.OK, latency_ms=100))
    for _ in range(4):
        opt._record_transport("lo", "batch", _call("lo", CallStatus.ERROR))
    opt._record_transport("lo", "batch", _call("lo", CallStatus.OK, latency_ms=100))
    policy = OptimizerPolicy(opt)
    result = policy.select([_cfg("lo"), _cfg("hi")], operation="batch")
    assert result is not None
    assert result.name == "hi"


def test_optimizer_policy_interactive_ranks_by_latency():
    opt = Optimizer(min_sample_count=5, exploration_fraction=0.0)
    # Both have identical good quality; "fast" has lower latency
    for _ in range(5):
        opt._record_transport("fast", "op", _call("fast", CallStatus.OK, latency_ms=10))
    for _ in range(5):
        opt._record_transport("slow", "op", _call("slow", CallStatus.OK, latency_ms=500))
    policy = OptimizerPolicy(opt)
    result = policy.select([_cfg("slow"), _cfg("fast")], operation="op")
    assert result is not None
    assert result.name == "fast"


def test_optimizer_policy_exploration_bypasses_ranking():
    # With exploration_fraction=1.0, select is random — run enough times to observe non-determinism
    opt = Optimizer(min_sample_count=5, exploration_fraction=1.0)
    for _ in range(5):
        opt._record_transport("fast", "op", _call("fast", CallStatus.OK, latency_ms=1))
    for _ in range(5):
        opt._record_transport("slow", "op", _call("slow", CallStatus.OK, latency_ms=9999))
    policy = OptimizerPolicy(opt)
    candidates = [_cfg("fast"), _cfg("slow")]
    seen = {policy.select(candidates, operation="op").name for _ in range(50)}
    assert "slow" in seen


def test_optimizer_policy_unknown_latency_ranked_last_interactive():
    opt = Optimizer(min_sample_count=5, exploration_fraction=0.0)
    # "known" has measured latency; "unknown" has no OK calls → latency=inf
    for _ in range(5):
        opt._record_transport("known", "op", _call("known", CallStatus.OK, latency_ms=999))
    policy = OptimizerPolicy(opt)
    result = policy.select([_cfg("unknown"), _cfg("known")], operation="op")
    assert result is not None
    assert result.name == "known"


# ---------------------------------------------------------------------------
# Pool acquire()/policy interaction — see tests/test_pool.py for coverage of
# slot selection, round-robin, and policy wiring.
# ---------------------------------------------------------------------------

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
            pool.release = AsyncMock()
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
    slot = broker._pool._slots[name]
    slot.cooldown_until = datetime.now(UTC) + timedelta(seconds=300)
    slot.fail_count = 1


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
            # Occupy the only slot so the next acquire raises TimeoutError → NoLLMAvailableError.
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


# ---------------------------------------------------------------------------
# Quality aggregate + derived demotions (d_quality = 35/37)
# ---------------------------------------------------------------------------


def test_quality_aggregate_matches_hand_computed_values():
    """Hand-computed (d=35/37) for ratings [0.8, 0.6, 1.0]:
    weight~=2.8408, weighted_good~=2.2834, weight_sq~=2.6955, count=3 (un-decayed)."""
    opt = Optimizer()
    for score in (0.8, 0.6, 1.0):
        opt._record_quality("x", "op", score)
    summary = opt._quality[("x", "op")]
    assert summary.count == 3
    assert summary.weight == pytest.approx(2.840760, abs=1e-5)
    assert summary.weighted_good == pytest.approx(2.283419, abs=1e-5)
    assert summary.weight_sq == pytest.approx(2.695505, abs=1e-5)


def _saturate_quality(
    opt: Optimizer,
    name: str,
    operation: str | None,
    score: float,
    n: int = 500,
) -> None:
    for _ in range(n):
        opt._record_quality(name, operation, score)


def test_evaluate_demotions_all_evidenced_ops_bad_globally_demoted():
    opt = Optimizer()
    _saturate_quality(opt, "x", "op_a", 0.1)
    _saturate_quality(opt, "x", "op_b", 0.05)
    demotions = opt.evaluate_demotions("x")
    assert demotions == {"op_a": True, "op_b": True}
    assert opt.is_globally_demoted("x") is True


def test_evaluate_demotions_mixed_only_bad_op_demoted():
    opt = Optimizer()
    _saturate_quality(opt, "x", "op_a", 0.1)  # bad
    _saturate_quality(opt, "x", "op_b", 0.9)  # good
    demotions = opt.evaluate_demotions("x")
    assert demotions == {"op_a": True, "op_b": False}
    assert opt.is_globally_demoted("x") is False


def test_evaluate_demotions_below_min_count_nothing_demoted():
    opt = Optimizer()
    for _ in range(5):  # well below quality_effective_n=36
        opt._record_quality("x", "op", 0.05)
    demotions = opt.evaluate_demotions("x")
    assert demotions == {"op": False}
    assert opt.is_globally_demoted("x") is False


def test_evaluate_demotions_no_evidence_returns_empty_and_not_globally_demoted():
    opt = Optimizer()
    assert opt.evaluate_demotions("ghost") == {}
    assert opt.is_globally_demoted("ghost") is False


def test_is_globally_demoted_ignores_under_evidenced_operation():
    """Regression: a model confidently demoted on one operation must still be flagged
    globally demoted even though a brand-new operation has too few samples to judge —
    the under-evidenced operation must not count as "not demoted" and mask the rest."""
    opt = Optimizer()
    _saturate_quality(opt, "x", "op_a", 0.05)  # confidently bad, sufficient evidence
    for _ in range(5):  # well below quality_effective_n=36 — insufficient evidence
        opt._record_quality("x", "op_b", 0.95)
    demotions = opt.evaluate_demotions("x")
    assert demotions == {"op_a": True, "op_b": False}  # op_b's False masks "no evidence"
    assert opt.is_globally_demoted("x") is True


def test_decision_band_floor_minus_margin_trips_bound_floor_does_not():
    """Hand-computed at saturation (n_eff~=36, z~=1.9600 for 95% confidence):
    p=quality_floor-quality_margin=0.15 -> Wilson upper~=0.2996 < 0.3 (demoted);
    p=quality_floor=0.3 -> Wilson upper~=0.4629 >= 0.3 (never demoted)."""
    opt = Optimizer(quality_floor=0.3, quality_margin=0.15, quality_confidence=0.95)
    _saturate_quality(opt, "bad", "op", 0.15, n=2000)
    _saturate_quality(opt, "good", "op", 0.30, n=2000)
    assert opt.evaluate_demotions("bad") == {"op": True}
    assert opt.evaluate_demotions("good") == {"op": False}


def test_manual_latch_suppresses_evaluate_demotions_entirely():
    opt = Optimizer()
    _saturate_quality(opt, "x", "op", 0.05)
    assert opt.evaluate_demotions("x") == {"op": True}
    opt.set_benched("x")
    assert opt.evaluate_demotions("x") == {}
    assert opt.is_globally_demoted("x") is False
    opt.clear_benched("x")
    assert opt.evaluate_demotions("x") == {"op": True}


def test_demoted_op_with_no_further_events_keeps_frozen_aggregate():
    opt = Optimizer()
    _saturate_quality(opt, "x", "op", 0.05)
    summary = opt._quality[("x", "op")]
    before = (summary.weight, summary.weighted_good, summary.weight_sq, summary.count)
    assert opt.evaluate_demotions("x") == {"op": True}
    assert opt.evaluate_demotions("x") == {"op": True}
    after = (summary.weight, summary.weighted_good, summary.weight_sq, summary.count)
    assert before == after


def test_derived_recovery_globally_demoted_model_rated_well_on_new_operation():
    """No explicit un-demote call — new evidence on an untried operation changes the derivation."""
    opt = Optimizer()
    _saturate_quality(opt, "x", "op_a", 0.05)
    assert opt.is_globally_demoted("x") is True
    _saturate_quality(opt, "x", "op_b", 0.95)
    assert opt.evaluate_demotions("x") == {"op_a": True, "op_b": False}
    assert opt.is_globally_demoted("x") is False


def test_reset_quality_clears_all_operations_for_model():
    opt = Optimizer()
    _saturate_quality(opt, "x", "op_a", 0.05)
    _saturate_quality(opt, "x", "op_b", 0.05)
    opt._record_quality("y", "op_a", 0.05)
    opt.reset_quality("x")
    assert opt.evaluate_demotions("x") == {}
    assert ("y", "op_a") in opt._quality


# ---------------------------------------------------------------------------
# load_summaries / to_profile round trip
# ---------------------------------------------------------------------------


def test_to_profile_and_load_summaries_round_trip_reproduces_same_bounds():
    opt = Optimizer()
    for _ in range(20):
        opt._record_transport("x", "op", _call("x", CallStatus.OK, latency_ms=100))
    _saturate_quality(opt, "x", "op", 0.05)
    demotions_before = opt.evaluate_demotions("x")
    rate_before = opt.usable_rate("x", "op")
    latency_before = opt.mean_latency_ms("x", "op")

    profile = opt.to_profile("x")

    opt2 = Optimizer()
    opt2.load_summaries("x", profile)
    assert opt2.evaluate_demotions("x") == demotions_before
    assert opt2.usable_rate("x", "op") == pytest.approx(rate_before)
    assert opt2.mean_latency_ms("x", "op") == pytest.approx(latency_before)


# ---------------------------------------------------------------------------
# record_quality via OptimizerTelemetry: call_id -> (name, operation) resolution
# ---------------------------------------------------------------------------


def test_record_quality_updates_the_right_name_and_operation():
    async def run():
        pool = LLMPool(state_store=None, user_id=None)
        await pool.add(_cfg(), "key")
        opt = Optimizer()
        opt_tel = _opt_tel(opt, pool)

        call = _call("x", CallStatus.OK, operation="summarize")
        await opt_tel.record(call)
        await opt_tel.record_quality(call.id, 0.8)

        summary = opt._quality[("x", "summarize")]
        assert summary.count == 1
        assert summary.weighted_good == pytest.approx(0.8)

    asyncio.run(run())


def test_record_quality_unknown_call_id_warns_and_is_dropped(caplog):
    async def run():
        pool = LLMPool(state_store=None, user_id=None)
        await pool.add(_cfg(), "key")
        opt = Optimizer()
        opt_tel = _opt_tel(opt, pool)

        with caplog.at_level("WARNING"):
            await opt_tel.record_quality("never-recorded", 0.8)

        assert opt._quality == {}
        assert any("not indexed" in r.message for r in caplog.records)

    asyncio.run(run())


def test_call_index_never_exceeds_cap_under_sustained_traffic():
    async def run():
        pool = LLMPool(state_store=None, user_id=None)
        await pool.add(_cfg(), "key")
        opt = Optimizer()
        opt_tel = _opt_tel(opt, pool)

        for i in range(_CALL_INDEX_CAP + 500):
            await opt_tel.record(
                Call(
                    id=f"call-{i}",
                    llm_name="x",
                    operation=None,
                    trace_id=None,
                    status=CallStatus.OK,
                )
            )

        assert len(opt_tel._call_index) <= _CALL_INDEX_CAP

    asyncio.run(run())

"""Tests for Optimizer failure bookkeeping, quality windows, and OptimizerTelemetry."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from llmbroker.broker import AsyncBroker
from llmbroker.broker.pool import LLMPool
from llmbroker.exceptions import NoLLMAvailableError
from llmbroker.models import (
    Call,
    CallStatus,
    LLMConfig,
    LLMMetrics,
)
from llmbroker.optimizer import (
    _CALL_INDEX_CAP,
    Optimizer,
    OptimizerTelemetry,
    wilson_upper,
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
    http_status: int | None = None,
) -> Call:
    return Call(
        id="test",
        llm_name=name,
        operation=operation,
        trace_id=None,
        status=status,
        latency_ms=latency_ms,
        http_status=http_status,
    )


def _registry(tmp_path, name="p1"):
    f = tmp_path / "llms.toml"
    f.write_text(f'[[llms]]\nname="{name}"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n')
    return Registry(f)


def _opt_tel(opt: Optimizer, pool: LLMPool, telemetry=None) -> OptimizerTelemetry:
    return OptimizerTelemetry(opt, telemetry or NoTelemetry(), pool)


# ---------------------------------------------------------------------------
# Optimizer unit tests: backoff bookkeeping
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
# Dead-key handling (401/403 drop) via OptimizerTelemetry
# ---------------------------------------------------------------------------


def test_auth_failure_401_drops_llm_and_logs(caplog):
    async def run():
        pool = LLMPool(state_store=None, user_id=None)
        await pool.add(_cfg(), "key")
        opt = Optimizer()
        opt_tel = _opt_tel(opt, pool)

        with caplog.at_level("ERROR"):
            await opt_tel.record(_call("x", CallStatus.ERROR, http_status=401))

        assert "x" not in pool
        assert any("API key" in r.message and "401" in r.message for r in caplog.records)
        alerts = opt.alerts()
        assert len(alerts) == 1
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


def test_generic_error_does_not_drop_llm():
    async def run():
        pool = LLMPool(state_store=None, user_id=None)
        await pool.add(_cfg(), "key")
        opt = Optimizer()
        opt_tel = _opt_tel(opt, pool)

        for _ in range(3):
            await opt_tel.record(_call("x", CallStatus.ERROR))

        assert "x" in pool
        assert opt.rl_fail_count("x") == 3

    asyncio.run(run())


def test_ok_calls_reset_rl_fail_count():
    async def run():
        pool = LLMPool(state_store=None, user_id=None)
        await pool.add(_cfg(), "key")
        opt = Optimizer()
        opt_tel = _opt_tel(opt, pool)

        await opt_tel.record(_call("x", CallStatus.RATE_LIMITED))
        assert opt.rl_fail_count("x") == 1
        await opt_tel.record(_call("x", CallStatus.OK))
        assert opt.rl_fail_count("x") == 0

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
# wilson_upper() + quality windows / demotion verdicts
# ---------------------------------------------------------------------------


def test_wilson_upper_all_good_high_bound():
    bound = wilson_upper([1.0] * 30, 1.96)
    assert bound == pytest.approx(1.0, abs=1e-4)


def test_wilson_upper_all_bad_low_bound():
    bound = wilson_upper([0.0] * 30, 1.96)
    assert bound < 0.2


def test_is_demoted_false_below_min_count():
    opt = Optimizer(quality_min_count=10)
    for _ in range(9):
        opt.record_quality("x", None, 0.0)
    assert opt.is_demoted("x", None) is False


def test_is_demoted_true_once_min_count_reached_with_bad_scores():
    opt = Optimizer(quality_min_count=10, quality_floor=0.3)
    for _ in range(10):
        opt.record_quality("x", None, 0.0)
    assert opt.is_demoted("x", None) is True


def test_is_demoted_false_for_unknown_model():
    opt = Optimizer()
    assert opt.is_demoted("ghost", None) is False


def test_window_evicts_oldest_beyond_quality_window():
    opt = Optimizer(quality_window=5, quality_min_count=1, quality_floor=0.5)
    for score in (0.0, 0.0, 0.0, 0.0, 0.0):
        opt.record_quality("x", None, score)
    assert opt.is_demoted("x", None) is True
    for _ in range(5):
        opt.record_quality("x", None, 1.0)  # evicts every 0.0
    assert opt.is_demoted("x", None) is False


def test_demoted_operations_only_lists_demoted_ops():
    opt = Optimizer(quality_min_count=10, quality_floor=0.3)
    for _ in range(10):
        opt.record_quality("x", "bad_op", 0.0)
        opt.record_quality("x", "good_op", 1.0)
    assert opt.demoted_operations("x") == frozenset({"bad_op"})


def test_record_quality_logs_flip_warning_then_info(caplog):
    opt = Optimizer(quality_min_count=10, quality_floor=0.3)
    with caplog.at_level("WARNING", logger="llmbroker.broker"):
        for _ in range(10):
            opt.record_quality("x", "op", 0.0)
    assert any("quality-demoted" in r.message for r in caplog.records)

    caplog.clear()
    with caplog.at_level("INFO", logger="llmbroker.broker"):
        for _ in range(10):
            opt.record_quality("x", "op", 1.0)
    assert any("demotion cleared" in r.message for r in caplog.records)


def test_load_scores_replaces_windows_wholesale():
    opt = Optimizer(quality_min_count=10, quality_floor=0.3)
    opt.load_scores({("x", "op"): [0.0] * 10})
    assert opt.is_demoted("x", "op") is True
    opt.load_scores({("x", "op"): [1.0] * 10})
    assert opt.is_demoted("x", "op") is False


def test_load_scores_truncates_to_quality_window():
    opt = Optimizer(quality_window=3, quality_min_count=1)
    opt.load_scores({("x", "op"): [0.0, 0.0, 0.0, 1.0, 1.0, 1.0]})
    assert len(opt._scores[("x", "op")]) == 3
    assert list(opt._scores[("x", "op")]) == [1.0, 1.0, 1.0]


# ---------------------------------------------------------------------------
# Router and Broker wiring tests
# ---------------------------------------------------------------------------


def test_broker_no_optimizer_when_optimize_false(tmp_path):
    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path),
            telemetry=NoTelemetry(),
            optimize=False,
        ) as broker:
            assert broker._optimizer is None
            assert broker._router._optimizer is None

    asyncio.run(run())


def test_broker_demoted_model_still_serves_as_last_resort(tmp_path):
    """A quality-demoted model with no alternative is still acquired — soft demotion."""

    async def run():
        opt = Optimizer(quality_min_count=10, quality_floor=0.3)
        for _ in range(10):
            opt.record_quality("p1", None, 0.0)
        async with AsyncBroker(
            registry=_registry(tmp_path),
            secrets=DictSecrets({"K": "test"}),
            telemetry=NoTelemetry(),
            optimize=opt,
        ) as broker:
            picked = await broker._pool.acquire(0, operation=None)
            assert picked.name == "p1"

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

        window = opt._scores[("x", "summarize")]
        assert list(window) == [0.8]

    asyncio.run(run())


def test_record_quality_unknown_call_id_warns_and_is_dropped(caplog):
    async def run():
        pool = LLMPool(state_store=None, user_id=None)
        await pool.add(_cfg(), "key")
        opt = Optimizer()
        opt_tel = _opt_tel(opt, pool)

        with caplog.at_level("WARNING"):
            await opt_tel.record_quality("never-recorded", 0.8)

        assert opt._scores == {}
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

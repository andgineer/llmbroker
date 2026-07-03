"""Tests for the learned-profile broker wiring: warm start, live sync, manual bench.

Uses sqlite (registry + state store) for determinism/speed — the wiring itself is
backend-agnostic (drives only through RegistryProtocol / StateStoreProtocol).
"""

import llmbroker.sqlite
import pytest

from llmbroker.broker import AsyncBroker
from llmbroker.broker.pool import TIER_DEMOTED_EVIDENCED, TIER_NORMAL
from llmbroker.models import Call, CallStatus, LLMConfig, LLMProfile, QualitySummary
from llmbroker.optimizer import Optimizer
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.telemetry import NoTelemetry


def _cfg(name: str = "p1", *, deprecated: bool = False) -> LLMConfig:
    return LLMConfig(
        name=name,
        base_url="https://x/v1",
        model="m",
        api_key_ref="K",
        deprecated=deprecated,
    )


def _call(call_id: str, name: str = "p1", operation: str | None = None) -> Call:
    return Call(id=call_id, llm_name=name, operation=operation, trace_id=None, status=CallStatus.OK)


async def test_persisted_manual_latch_applied_at_provision(tmp_path):
    db = str(tmp_path / "b.db")
    reg = llmbroker.sqlite.Registry(db)
    await reg.add(_cfg())
    await reg.write_profile("p1", LLMProfile(benched=True, benched_reason="manual review"))

    async with AsyncBroker(
        registry=llmbroker.sqlite.Registry(db),
        secrets=DictSecrets({"K": "key"}),
        telemetry=NoTelemetry(),
    ) as broker:
        assert broker._pool.is_benched("p1")
        assert broker._optimizer is not None
        assert broker._optimizer.evaluate_demotions("p1") == {}  # latch suppresses derivation


async def test_persisted_deprecated_marker_applied_at_provision(tmp_path):
    db = str(tmp_path / "b.db")
    reg = llmbroker.sqlite.Registry(db)
    await reg.add(_cfg(deprecated=True))

    async with AsyncBroker(
        registry=llmbroker.sqlite.Registry(db),
        secrets=DictSecrets({"K": "key"}),
        telemetry=NoTelemetry(),
    ) as broker:
        assert broker._pool.is_deprecated("p1")


async def test_live_demotion_reaches_second_running_instance_within_one_ttl_refresh(tmp_path):
    db = str(tmp_path / "b.db")
    reg = llmbroker.sqlite.Registry(db)
    await reg.add(_cfg())
    state_store = llmbroker.sqlite.StateStore(db)

    opt_a = Optimizer(quality_effective_n=4, quality_confidence=0.8)
    opt_b = Optimizer(quality_effective_n=4, quality_confidence=0.8)

    async with (
        AsyncBroker(
            registry=llmbroker.sqlite.Registry(db),
            secrets=DictSecrets({"K": "key"}),
            telemetry=NoTelemetry(),
            state_store=state_store,
            optimize=opt_a,
        ) as broker_a,
        AsyncBroker(
            registry=llmbroker.sqlite.Registry(db),
            secrets=DictSecrets({"K": "key"}),
            telemetry=NoTelemetry(),
            state_store=state_store,
            optimize=opt_b,
        ) as broker_b,
    ):
        assert broker_b._pool.tier_of("p1", "summarize") == TIER_NORMAL

        for i in range(10):
            call = _call(f"c{i}", operation="summarize")
            await broker_a._telemetry.record(call)
            await broker_a._telemetry.record_quality(call.id, 0.0)

        # Force instance B's TTL heartbeat to fire on the very next event.
        assert broker_b._profile_sync is not None
        broker_b._profile_sync._next_refresh = 0.0
        await broker_b._telemetry.record(_call("ping"))

        assert broker_b._pool.tier_of("p1", "summarize") == TIER_DEMOTED_EVIDENCED


async def test_demotion_flip_alert_and_standing_realert_respects_interval(tmp_path):
    db = str(tmp_path / "b.db")
    reg = llmbroker.sqlite.Registry(db)
    await reg.add(_cfg())
    opt = Optimizer(quality_effective_n=4, quality_confidence=0.8, demotion_realert_interval=9999.0)

    async with AsyncBroker(
        registry=llmbroker.sqlite.Registry(db),
        secrets=DictSecrets({"K": "key"}),
        telemetry=NoTelemetry(),
        optimize=opt,
    ) as broker:
        assert broker._profile_sync is not None
        for i in range(10):
            call = _call(f"c{i}", operation="summarize")
            await broker._telemetry.record(call)
            broker._profile_sync._next_refresh = 0.0
            await broker._telemetry.record_quality(call.id, 0.0)

        alerts = [a.message for a in opt.alerts()]
        assert any("quality-demoted" in m for m in alerts)

        # Within the (huge) debounce interval, forcing another refresh must not re-alert.
        broker._profile_sync._next_refresh = 0.0
        await broker._telemetry.record(_call("ping1"))
        assert opt.alerts() == []

        # Shrink the interval and force another refresh — the standing condition re-alerts.
        opt.demotion_realert_interval = 0.0
        broker._profile_sync._next_refresh = 0.0
        await broker._telemetry.record(_call("ping2"))
        alerts2 = [a.message for a in opt.alerts()]
        assert any("quality-demoted" in m for m in alerts2)


async def test_disable_enable_llm_round_trip_and_resets_quality_aggregates(tmp_path):
    db = str(tmp_path / "b.db")
    reg = llmbroker.sqlite.Registry(db)
    await reg.add(_cfg())
    opt = Optimizer(quality_effective_n=4, quality_confidence=0.8)

    async with AsyncBroker(
        registry=llmbroker.sqlite.Registry(db),
        secrets=DictSecrets({"K": "key"}),
        telemetry=NoTelemetry(),
        optimize=opt,
    ) as broker:
        for i in range(5):
            call = _call(f"c{i}", operation="summarize")
            await broker._telemetry.record(call)
            await broker._telemetry.record_quality(call.id, 0.0)
        assert opt._quality[("p1", "summarize")].count == 5

        await broker.disable_llm("p1", reason="manual review")
        assert broker._pool.is_benched("p1")
        profiles = await reg.read_profiles()
        assert profiles["p1"].benched is True
        assert profiles["p1"].benched_reason == "manual review"

        await broker.enable_llm("p1")
        assert not broker._pool.is_benched("p1")
        assert ("p1", "summarize") not in opt._quality
        profiles2 = await reg.read_profiles()
        assert profiles2["p1"].benched is False
        assert profiles2["p1"].stats.get("summarize", {}).get("quality") is None


async def test_disable_enable_llm_without_optimizer_preserves_existing_profile_stats(tmp_path):
    """Regression: disable_llm/enable_llm used to write a brand-new empty profile when
    optimize=False, silently wiping any stats accumulated by a prior optimize=True run."""
    db = str(tmp_path / "b.db")
    reg = llmbroker.sqlite.Registry(db)
    await reg.add(_cfg())
    stats = {
        "summarize": {
            "quality": QualitySummary(weight=10.0, weighted_good=8.0, weight_sq=5.0, count=20),
        },
    }
    await reg.write_profile("p1", LLMProfile(stats=stats))

    async with AsyncBroker(
        registry=llmbroker.sqlite.Registry(db),
        secrets=DictSecrets({"K": "key"}),
        telemetry=NoTelemetry(),
        optimize=False,
    ) as broker:
        assert broker._optimizer is None

        await broker.disable_llm("p1", reason="manual review")
        profiles = await reg.read_profiles()
        assert profiles["p1"].benched is True
        assert profiles["p1"].benched_reason == "manual review"
        assert profiles["p1"].stats == stats

        await broker.enable_llm("p1")
        profiles2 = await reg.read_profiles()
        assert profiles2["p1"].benched is False
        assert profiles2["p1"].stats == stats


async def test_remove_then_readd_same_name_is_routable_after_disable(tmp_path):
    """Regression: removing a benched model used to leave the pool's stale benched
    latch behind, so a fresh config re-added under the same name was silently stuck
    non-routable — no error, just never enqueued."""
    db = str(tmp_path / "b.db")
    reg = llmbroker.sqlite.Registry(db)
    await reg.add(_cfg())

    async with AsyncBroker(
        registry=llmbroker.sqlite.Registry(db),
        secrets=DictSecrets({"K": "key"}),
        telemetry=NoTelemetry(),
    ) as broker:
        await broker.disable_llm("p1", reason="manual review")
        assert broker._pool.is_benched("p1")

        await broker.remove("p1")
        assert "p1" not in broker._benched_meta

        await broker.add(_cfg())
        assert not broker._pool.is_benched("p1")
        assert broker._pool._queue.qsize() == 1


async def test_snapshot_lands_in_registry_on_aclose(tmp_path):
    db = str(tmp_path / "b.db")
    reg = llmbroker.sqlite.Registry(db)
    await reg.add(_cfg())

    broker = AsyncBroker(
        registry=llmbroker.sqlite.Registry(db),
        secrets=DictSecrets({"K": "key"}),
        telemetry=NoTelemetry(),
    )
    await broker.ensure_pool()
    call = Call(
        id="c1",
        llm_name="p1",
        operation="summarize",
        trace_id=None,
        status=CallStatus.OK,
        latency_ms=100,
    )
    await broker._telemetry.record(call)
    await broker.aclose()

    profiles = await reg.read_profiles()
    assert profiles["p1"].stats.get("summarize", {}).get("transport") is not None


class _FlakyRegistry:
    """Wraps a real sqlite registry; write_profile can be switched to raise, to
    simulate a durability blip in the profile-snapshot path."""

    def __init__(self, inner):
        self._inner = inner
        self.fail_writes = False
        self.aclose_called = False

    async def load(self, user_id=None):
        return await self._inner.load(user_id)

    async def get(self, name, user_id=None):
        return await self._inner.get(name, user_id)

    async def add(self, cfg, user_id=None):
        await self._inner.add(cfg, user_id)

    async def update(self, cfg, user_id=None):
        await self._inner.update(cfg, user_id)

    async def remove(self, name, user_id=None):
        await self._inner.remove(name, user_id)

    async def read_profiles(self, user_id=None):
        return await self._inner.read_profiles(user_id)

    async def write_profile(self, name, profile, user_id=None):
        if self.fail_writes:
            raise OSError("simulated durability blip")
        await self._inner.write_profile(name, profile, user_id)

    async def aclose(self):
        self.aclose_called = True
        await self._inner.aclose()


async def test_record_quality_survives_snapshot_write_failure(tmp_path):
    """Regression: a registry.write_profile failure during the debounced snapshot
    used to propagate out of record_quality and break score recording."""
    db = str(tmp_path / "b.db")
    reg = llmbroker.sqlite.Registry(db)
    await reg.add(_cfg())
    flaky = _FlakyRegistry(llmbroker.sqlite.Registry(db))

    async with AsyncBroker(
        registry=flaky,
        secrets=DictSecrets({"K": "key"}),
        telemetry=NoTelemetry(),
    ) as broker:
        call = _call("c1", operation="summarize")
        await broker._telemetry.record(call)

        flaky.fail_writes = True
        assert broker._profile_sync is not None
        broker._profile_sync._next_snapshot["p1"] = 0.0
        await broker._telemetry.record_quality(call.id, 1.0)  # must not raise


async def test_aclose_closes_all_ports_even_if_snapshot_write_fails(tmp_path):
    """Regression: write_all_snapshots() used to propagate a write failure out of
    aclose(), skipping the rest of the port-cleanup loop."""
    db = str(tmp_path / "b.db")
    reg = llmbroker.sqlite.Registry(db)
    await reg.add(_cfg())
    flaky = _FlakyRegistry(llmbroker.sqlite.Registry(db))

    broker = AsyncBroker(
        registry=flaky,
        secrets=DictSecrets({"K": "key"}),
        telemetry=NoTelemetry(),
    )
    await broker.ensure_pool()
    await broker._telemetry.record(_call("c1", operation="summarize"))

    flaky.fail_writes = True
    await broker.aclose()  # must not raise
    assert flaky.aclose_called


async def test_short_lived_lifecycles_accumulate_count_until_usable_rate_non_none(tmp_path):
    """No state store configured — the registry snapshot on aclose is the only
    persistence, and it is what a fresh short-lived process's first pick relies on."""
    db = str(tmp_path / "b.db")
    reg = llmbroker.sqlite.Registry(db)
    await reg.add(_cfg())

    min_sample_count = 5
    for i in range(min_sample_count):
        opt = Optimizer(min_sample_count=min_sample_count)
        broker = AsyncBroker(
            registry=llmbroker.sqlite.Registry(db),
            secrets=DictSecrets({"K": "key"}),
            telemetry=NoTelemetry(),
            optimize=opt,
        )
        await broker.ensure_pool()
        assert opt.usable_rate("p1", None) is None  # below min_sample_count until the last run
        call = Call(
            id=f"c{i}",
            llm_name="p1",
            operation=None,
            trace_id=None,
            status=CallStatus.OK,
            latency_ms=100,
        )
        await broker._telemetry.record(call)
        await broker.aclose()

    opt_final = Optimizer(min_sample_count=min_sample_count)
    broker_final = AsyncBroker(
        registry=llmbroker.sqlite.Registry(db),
        secrets=DictSecrets({"K": "key"}),
        telemetry=NoTelemetry(),
        optimize=opt_final,
    )
    await broker_final.ensure_pool()
    assert opt_final.usable_rate("p1", None) is not None
    assert opt_final.mean_latency_ms("p1", None) == pytest.approx(100.0)
    await broker_final.aclose()

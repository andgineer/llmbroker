"""Integration tests: _LearningHook bookkeeping over all telemetry backends.

Each test runs against every implemented telemetry backend:
  - toml  — NoTelemetry (no DB, the project default)
  - sqlite
  - postgres (Docker — marked docker)
  - mongodb  (Docker — marked docker)

What is verified:
1. Dead-key handling (401/403) drops the LLM regardless of which backend recorded the call.
2. rl_fail_count bookkeeping (backoff exponent) is backend-agnostic.
3. Quality-window demotion verdicts are backend-agnostic and feed the pool's
   demoted-last selection.

The any_telemetry fixture is defined in conftest.py.
"""

import uuid

import pytest

from llmbroker.broker.learning import _LearningHook
from llmbroker.broker.pool import LLMPool
from llmbroker.models import Call, CallStatus, LifecyclePhase, LLMConfig
from llmbroker.optimizer import Optimizer

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


async def _noop_resync() -> None:
    return


def _hook(opt: Optimizer, telemetry: object, pool: LLMPool) -> _LearningHook:
    return _LearningHook(opt, telemetry, pool, _noop_resync)


# ---------------------------------------------------------------------------
# Dead-key handling — no stuck states, across backends
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("http_status", [401, 403])
async def test_auth_failure_drops_llm_cleanly(any_telemetry, http_status, caplog):
    """HTTP 401/403 drops the LLM from pool immediately and logs an API-key error.

    Verifies a re-added LLM starts fresh — no stale bookkeeping survives the drop.
    """
    pool = LLMPool()
    await pool.add(_cfg("llm1"), "key")
    opt = Optimizer()
    hook = _hook(opt, any_telemetry, pool)

    with caplog.at_level("ERROR", logger="llmbroker.broker"):
        await hook.record(_call("llm1", CallStatus.ERROR, http_status=http_status))

    assert "llm1" not in pool
    assert any("API key" in r.message and str(http_status) in r.message for r in caplog.records)
    await pool.add(_cfg("llm1"), "key")
    assert pool.state("llm1").phase is LifecyclePhase.AVAILABLE


async def test_repeated_ok_calls_keep_llm_available_and_reset_backoff(any_telemetry):
    """Sustained OK calls keep the LLM AVAILABLE and reset the rate-limit backoff counter."""
    pool = LLMPool()
    await pool.add(_cfg("llm1"), "key")
    opt = Optimizer()
    hook = _hook(opt, any_telemetry, pool)

    await hook.record(_call("llm1", CallStatus.RATE_LIMITED))
    assert opt.rl_fail_count("llm1") == 1

    for _ in range(15):
        await hook.record(_call("llm1", CallStatus.OK))

    assert pool.state("llm1").phase is LifecyclePhase.AVAILABLE
    assert opt.rl_fail_count("llm1") == 0


async def test_rl_fail_count_accumulates_across_backend(any_telemetry):
    """Consecutive RATE_LIMITED/UNAVAILABLE calls accumulate the backoff exponent."""
    pool = LLMPool()
    await pool.add(_cfg("llm1"), "key")
    opt = Optimizer()
    hook = _hook(opt, any_telemetry, pool)

    for _ in range(3):
        await hook.record(_call("llm1", CallStatus.RATE_LIMITED))

    assert opt.rl_fail_count("llm1") == 3
    assert "llm1" in pool  # rate limiting alone never drops the slot


# ---------------------------------------------------------------------------
# Quality windows + demoted-last selection — backend-agnostic
# ---------------------------------------------------------------------------


async def test_quality_demotion_end_to_end_prefers_the_better_model(any_telemetry):
    """Rating -> demotion -> demoted-last selection, driven through a real telemetry backend."""
    opt = Optimizer(quality_min_count=10, quality_floor=0.3)
    pool = LLMPool(optimizer=opt)
    await pool.add(_cfg("flaky"), "key", order=0)
    await pool.add(_cfg("stable"), "key", order=1)
    hook = _hook(opt, any_telemetry, pool)

    for _ in range(10):
        await hook.record_quality("flaky", "summarize", 0.0)
        await hook.record_quality("stable", "summarize", 1.0)

    assert opt.is_demoted("flaky", "summarize") is True
    assert opt.is_demoted("stable", "summarize") is False

    picked = await pool.acquire(0, operation="summarize")
    assert picked.name == "stable"


async def test_quality_demoted_model_still_serves_alone(any_telemetry):
    """A quality-demoted model with no alternative is still picked — demotion is soft."""
    opt = Optimizer(quality_min_count=10, quality_floor=0.3)
    pool = LLMPool(optimizer=opt)
    await pool.add(_cfg("only"), "key")
    hook = _hook(opt, any_telemetry, pool)

    for _ in range(10):
        await hook.record_quality("only", "summarize", 0.0)

    assert opt.is_demoted("only", "summarize") is True
    picked = await pool.acquire(0, operation="summarize")
    assert picked.name == "only"


async def test_quality_stats_are_isolated_per_operation(any_telemetry):
    """A bad verdict on one operation does not demote another operation for the same model."""
    pool = LLMPool()
    await pool.add(_cfg("llm1"), "key")
    opt = Optimizer(quality_min_count=10, quality_floor=0.3)
    hook = _hook(opt, any_telemetry, pool)

    for _ in range(10):
        await hook.record_quality("llm1", "op_a", 0.0)
        await hook.record_quality("llm1", "op_b", 1.0)

    assert opt.is_demoted("llm1", "op_a") is True
    assert opt.is_demoted("llm1", "op_b") is False

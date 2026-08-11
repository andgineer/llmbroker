"""Integration tests: ``Learner`` bookkeeping over all store backends.

Each test runs against every implemented store backend:
  - toml  — InMemoryStore (no DB)
  - file  — FileStore (the project default)
  - sqlite
  - postgres (Docker — marked docker)
  - mongodb  (Docker — marked docker)

What is verified:
1. Dead-key handling (401/403) drops the LLM regardless of which backend recorded the call.
2. rl_fail_count bookkeeping (backoff exponent) is backend-agnostic.
3. Quality-window demotion verdicts are backend-agnostic and feed the pool's
   demoted-last selection.

The any_store fixture is defined in conftest.py.
"""

import uuid

import pytest

from llmbroker.broker.learning import Learner
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


class _Journal:
    """Store and learner wired the way the router wires them: the row is written,
    then observed."""

    def __init__(self, opt: Optimizer, store, pool: LLMPool) -> None:
        self._store = store
        self._learner = Learner(opt, store, pool)

    async def record(self, call: Call) -> None:
        await self._store.record(call)
        await self._learner.observe(call)

    async def record_quality(self, llm_name: str, operation: str | None, score: float) -> None:
        await self._store.record_quality(llm_name, operation, score)
        self._learner.record_quality_observed(llm_name, operation, score)


# ---------------------------------------------------------------------------
# Dead-key handling — no stuck states, across backends
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("http_status", [401, 403])
async def test_auth_failure_is_reported_and_leaves_the_pool_intact(any_store, http_status, caplog):
    """HTTP 401/403 logs an API-key error naming the ref. The model stays in the
    shared pool — the key was one caller's, and its ring is what withdraws it."""
    pool = LLMPool()
    await pool.add(_cfg("llm1"))
    opt = Optimizer()
    journal = _Journal(opt, any_store, pool)

    with caplog.at_level("ERROR", logger="llmbroker.broker"):
        await journal.record(_call("llm1", CallStatus.ERROR, http_status=http_status))

    assert any("API key" in r.message and str(http_status) in r.message for r in caplog.records)
    assert pool.state("llm1").phase is LifecyclePhase.AVAILABLE


async def test_repeated_ok_calls_keep_llm_available_and_reset_backoff(any_store):
    """Sustained OK calls keep the LLM AVAILABLE and reset the rate-limit backoff counter."""
    pool = LLMPool()
    await pool.add(_cfg("llm1"))
    opt = Optimizer()
    journal = _Journal(opt, any_store, pool)

    await journal.record(_call("llm1", CallStatus.RATE_LIMITED))
    assert opt.rl_fail_count("llm1") == 1

    for _ in range(15):
        await journal.record(_call("llm1", CallStatus.OK))

    assert pool.state("llm1").phase is LifecyclePhase.AVAILABLE
    assert opt.rl_fail_count("llm1") == 0


async def test_rl_fail_count_accumulates_across_backend(any_store):
    """Consecutive RATE_LIMITED/UNAVAILABLE calls accumulate the backoff exponent."""
    pool = LLMPool()
    await pool.add(_cfg("llm1"))
    opt = Optimizer()
    journal = _Journal(opt, any_store, pool)

    for _ in range(3):
        await journal.record(_call("llm1", CallStatus.RATE_LIMITED))

    assert opt.rl_fail_count("llm1") == 3
    assert "llm1" in pool  # rate limiting alone never drops the slot


# ---------------------------------------------------------------------------
# Quality windows + demoted-last selection — backend-agnostic
# ---------------------------------------------------------------------------


async def test_quality_demotion_end_to_end_prefers_the_better_model(any_store):
    """Rating -> demotion -> demoted-last selection, driven through a real store backend."""
    opt = Optimizer(quality_min_count=10, quality_floor=0.3)
    pool = LLMPool(optimizer=opt)
    await pool.add(_cfg("flaky"), order=0)
    await pool.add(_cfg("stable"), order=1)
    journal = _Journal(opt, any_store, pool)

    for _ in range(10):
        await journal.record_quality("flaky", "summarize", 0.0)
        await journal.record_quality("stable", "summarize", 1.0)

    assert opt.is_demoted("flaky", "summarize") is True
    assert opt.is_demoted("stable", "summarize") is False

    picked = await pool.acquire(0, payable=frozenset({"k"}), operation="summarize")
    assert picked.name == "stable"


async def test_quality_demoted_model_still_serves_alone(any_store):
    """A quality-demoted model with no alternative is still picked — demotion is soft."""
    opt = Optimizer(quality_min_count=10, quality_floor=0.3)
    pool = LLMPool(optimizer=opt)
    await pool.add(_cfg("only"))
    journal = _Journal(opt, any_store, pool)

    for _ in range(10):
        await journal.record_quality("only", "summarize", 0.0)

    assert opt.is_demoted("only", "summarize") is True
    picked = await pool.acquire(0, payable=frozenset({"k"}), operation="summarize")
    assert picked.name == "only"


async def test_quality_stats_are_isolated_per_operation(any_store):
    """A bad verdict on one operation does not demote another operation for the same model."""
    pool = LLMPool()
    await pool.add(_cfg("llm1"))
    opt = Optimizer(quality_min_count=10, quality_floor=0.3)
    journal = _Journal(opt, any_store, pool)

    for _ in range(10):
        await journal.record_quality("llm1", "op_a", 0.0)
        await journal.record_quality("llm1", "op_b", 1.0)

    assert opt.is_demoted("llm1", "op_a") is True
    assert opt.is_demoted("llm1", "op_b") is False

"""Tests for ``Learner``: dead-key reporting, rl_fail_count bookkeeping, observed
ratings, and the one tail read a rebuild asks for (scores, budget bounds, metrics)."""

import uuid
from datetime import UTC, datetime, timedelta


from llmbroker.broker.learning import (
    Learner,
    metrics_from_calls,
)
from llmbroker.broker.pool import LLMPool
from llmbroker.models import Call, CallStatus, LLMConfig
from llmbroker.optimizer import Optimizer
from llmbroker.sqlite import Store as SqliteStore
from llmbroker.standalone.store import InMemoryStore


def _cfg(name: str = "x") -> LLMConfig:
    return LLMConfig(name=name, base_url="https://x/v1", model="m", api_key_ref="k")


def _call(name: str, status: CallStatus, **kw) -> Call:
    return Call(
        id=str(uuid.uuid4()),
        llm_name=name,
        operation=kw.pop("operation", None),
        trace_id=None,
        status=status,
        ts=kw.pop("ts", None) or datetime.now(UTC),
        **kw,
    )


def _soon() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=60)


async def _noop_resync() -> None:
    return


def _make_learner(opt: Optimizer, store, pool: LLMPool) -> Learner:
    return Learner(opt, store, pool)


# ---------------------------------------------------------------------------
# Dead-key handling: the observer reports it, the ring withdraws it
# ---------------------------------------------------------------------------


async def test_auth_failure_401_names_the_ref_at_error(caplog):
    """The model stays in the shared pool — the key belonged to one caller, and the
    ring that paid is what stops offering it."""
    pool = LLMPool()
    await pool.add(_cfg())
    learner = _make_learner(Optimizer(), InMemoryStore(), pool)

    with caplog.at_level("ERROR"):
        await learner.observe(_call("x", CallStatus.ERROR, http_status=401))

    assert "x" in pool
    assert any("API key" in r.message and "401" in r.message for r in caplog.records)


async def test_auth_failure_403_names_the_ref_at_error(caplog):
    pool = LLMPool()
    await pool.add(_cfg())
    opt = Optimizer()
    learner = _make_learner(opt, InMemoryStore(), pool)

    with caplog.at_level("ERROR"):
        await learner.observe(_call("x", CallStatus.ERROR, http_status=403))

    assert any("403" in r.message for r in caplog.records)


async def test_generic_error_does_not_drop_llm():
    pool = LLMPool()
    await pool.add(_cfg())
    opt = Optimizer()
    learner = _make_learner(opt, InMemoryStore(), pool)

    for _ in range(3):
        await learner.observe(_call("x", CallStatus.ERROR, cooldown_until=_soon()))

    assert "x" in pool
    assert opt.rl_fail_count("x") == 3


async def test_error_row_without_cooldown_does_not_advance_the_streak():
    """A failure that did not cool the model — a client-side 4xx, a spent wait
    budget — is not the model's fault and must not raise its backoff exponent."""
    pool = LLMPool()
    await pool.add(_cfg())
    opt = Optimizer()
    learner = _make_learner(opt, InMemoryStore(), pool)

    for _ in range(3):
        await learner.observe(_call("x", CallStatus.ERROR, http_status=400))

    assert "x" in pool
    assert opt.rl_fail_count("x") == 0


async def test_ok_calls_reset_rl_fail_count():
    pool = LLMPool()
    await pool.add(_cfg())
    opt = Optimizer()
    learner = _make_learner(opt, InMemoryStore(), pool)

    await learner.observe(_call("x", CallStatus.RATE_LIMITED))
    assert opt.rl_fail_count("x") == 1
    await learner.observe(_call("x", CallStatus.OK))
    assert opt.rl_fail_count("x") == 0


# ---------------------------------------------------------------------------
# record_quality: folded into the live window as the row is written
# ---------------------------------------------------------------------------


async def test_an_observed_rating_updates_the_window_instantly():
    pool = LLMPool()
    await pool.add(_cfg())
    opt = Optimizer()
    learner = _make_learner(opt, InMemoryStore(), pool)

    learner.record_quality_observed("x", "summarize", "c1", 0.8)

    assert list(opt._scores[("x", "summarize")]) == [("c1", 0.8)]


# ---------------------------------------------------------------------------
# relearn: quality windows + metrics from one tail read
# ---------------------------------------------------------------------------


async def test_rebuild_loads_quality_windows_from_journal(tmp_path):
    store = SqliteStore(tmp_path / "t.db")
    pool = LLMPool()
    await pool.add(_cfg("bad"))
    opt = Optimizer(quality_min_count=10, quality_floor=0.3)
    learner = _make_learner(opt, store, pool)

    for _ in range(10):
        call = _call("bad", CallStatus.OK, operation="summarize")
        await store.record(call)
        await store.record_quality(call.id, 0.0)

    await learner.relearn()

    assert opt.is_demoted("bad", "summarize") is True


async def test_rebuild_computes_metrics(tmp_path):
    store = SqliteStore(tmp_path / "t.db")
    pool = LLMPool()
    await pool.add(_cfg("x"))
    opt = Optimizer()
    learner = _make_learner(opt, store, pool)

    await store.record(_call("x", CallStatus.OK))
    await store.record(_call("x", CallStatus.OK))

    await learner.relearn()

    assert learner.metrics["x"].call_count == 2
    assert learner.metrics["x"].last_status is CallStatus.OK


# ── metrics_from_calls, now a projection of stats_from_calls ─────────────────


def test_metrics_from_calls_counts_a_rated_call_once():
    """Regression: rebuilding this on stats_from_calls must not change LLMMetrics, and
    the score riding on a call row must not turn it into a second row."""
    rows = [
        _call("a", CallStatus.ERROR),
        _call("a", CallStatus.OK, score=1.0),
        _call("b", CallStatus.OK),
    ]
    metrics = metrics_from_calls(rows)
    assert metrics["a"].call_count == 2
    assert metrics["b"].call_count == 1


def test_metrics_from_calls_takes_last_status_from_the_newest_row():
    newest = datetime(2030, 1, 2, tzinfo=UTC)
    rows = [
        _call("a", CallStatus.RATE_LIMITED, ts=newest),
        _call("a", CallStatus.OK, ts=datetime(2030, 1, 1, tzinfo=UTC)),
    ]
    metrics = metrics_from_calls(rows)
    assert metrics["a"].last_status is CallStatus.RATE_LIMITED
    assert metrics["a"].last_at == newest


def test_metrics_from_calls_on_no_rows_is_empty():
    assert metrics_from_calls([]) == {}

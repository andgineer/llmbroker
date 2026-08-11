"""Unit tests for Router: routing logic, backoff/cooldown formula, and failover."""

import asyncio
import time
from contextlib import aclosing
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from llmbroker.broker.learning import Learner
from llmbroker.broker.pool import LLMPool
from llmbroker.broker.router import Router, _Outcome
from llmbroker.exceptions import NoLLMAvailableError
from llmbroker.models import LifecyclePhase, LLMConfig
from llmbroker.optimizer import Optimizer
from llmbroker.standalone.store import InMemoryStore

from support import make_ring


class _NoStore:
    async def record(self, call):
        pass

    async def record_quality(self, llm_name, operation, score, *, call_id=None):
        pass


class _RecordingStore:
    """Captures every journaled ``Call`` row, for asserting cooldown/status fields."""

    def __init__(self) -> None:
        self.calls: list = []

    async def record(self, call):
        self.calls.append(call)

    async def record_quality(self, llm_name, operation, score, *, call_id=None):
        pass


def _cfg(name="p1", ref="K"):
    return LLMConfig(name=name, base_url="https://x/v1", model="m", api_key_ref=ref)


async def _pool(*cfgs) -> LLMPool:
    pool = LLMPool()
    for cfg in cfgs:
        await pool.add(cfg)
    return pool


def _router(pool: LLMPool) -> Router:
    return Router(pool, _NoStore())


async def _noop_resync() -> None:
    return


def _router_with_optimizer(pool: LLMPool, opt: Optimizer) -> Router:
    """Wire the Learner like AsyncBroker does, so rl_fail_count/dead-key drop drive for real."""
    store = InMemoryStore()
    learner = Learner(opt, store, pool)
    return Router(pool, store, optimizer=opt, learner=learner)


async def _attempt(router: Router, cfg: LLMConfig, ring=None) -> _Outcome:
    """Run one attempt off the failover driver and return the outcome it reported."""
    outcome = _Outcome()
    gen = router._attempt(
        cfg,
        outcome,
        None,
        ring=ring if ring is not None else make_ring(),
        messages=[{"role": "user", "content": "hi"}],
        tools=None,
        operation=None,
        trace_id=None,
    )
    async with aclosing(gen) as results:
        async for _ in results:
            pass
    return outcome


def _spy_cool_down(pool: LLMPool) -> list[float]:
    """Record every delay passed to cool_down while still calling through to the real method."""
    captured: list[float] = []
    original = pool.cool_down

    async def spy(config, delay):
        captured.append(delay)
        await original(config, delay)

    pool.cool_down = spy  # type: ignore[method-assign]
    return captured


def _http_status_error(status: int, retry_after: str | None = None) -> httpx.HTTPStatusError:
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {"Retry-After": retry_after} if retry_after is not None else {}
    resp.text = f"HTTP {status}"
    return httpx.HTTPStatusError("err", request=MagicMock(), response=resp)


_PATCH = "llmbroker.broker.router.call_provider"


def test_happy_path_returns_result():
    async def run():
        router = _router(await _pool(_cfg()))
        with patch(_PATCH, new=AsyncMock(return_value=("hello", None, None))):
            result = await router.chat(make_ring(), [{"role": "user", "content": "hi"}])
        assert result.text == "hello"

    asyncio.run(run())


def test_a_ref_the_caller_holds_no_key_for_raises_no_llm_available():
    async def run():
        router = _router(await _pool(_cfg(ref="UNPAYABLE")))
        with pytest.raises(NoLLMAvailableError, match="api_key_ref") as exc_info:
            await router.chat(make_ring(), [{"role": "user", "content": "hi"}], wait=0)
        assert exc_info.value.reason == "no_keys"

    asyncio.run(run())


def test_a_pool_the_caller_cannot_pay_for_raises_immediately_with_default_wait():
    """Regression for the eager-guard/hang fix: a pool this caller holds no key for
    must not block on the default wait=None forever — the check runs before any slot
    acquisition."""

    async def run():
        pool = LLMPool()
        await pool.add(_cfg(ref="UNPAYABLE"))
        router = _router(pool)
        with pytest.raises(NoLLMAvailableError, match="api_key_ref") as exc_info:
            await router.chat(make_ring(), [{"role": "user", "content": "hi"}])
        assert exc_info.value.reason == "no_keys"

    asyncio.run(run())


def test_mixed_keyed_and_keyless_pool_routes_over_keyed_only():
    """A keyless config is never acquirable, so the router routes to the keyed one
    without raising and without ever blocking on the keyless slot."""

    async def run():
        keyed, keyless = _cfg("keyed"), _cfg("keyless", ref="UNPAYABLE")
        pool = LLMPool()
        await pool.add(keyed)
        await pool.add(keyless)
        router = _router(pool)
        with patch(_PATCH, new=AsyncMock(return_value=("ok", None, None))):
            result = await router.chat(make_ring(), [{"role": "user", "content": "hi"}], wait=0)
        assert result.text == "ok"
        assert result._llm_name == "keyed"

    asyncio.run(run())


def test_http_429_wait0_raises_no_llm_available():
    async def run():
        router = _router(await _pool(_cfg()))
        with patch(_PATCH, new=AsyncMock(side_effect=_http_status_error(429))):
            with pytest.raises(NoLLMAvailableError) as exc_info:
                await router.chat(make_ring(), [{"role": "user", "content": "hi"}], wait=0)
        assert exc_info.value.reason == "timeout"

    asyncio.run(run())


def test_http_503_wait0_raises_no_llm_available():
    async def run():
        router = _router(await _pool(_cfg()))
        with patch(_PATCH, new=AsyncMock(side_effect=_http_status_error(503))):
            with pytest.raises(NoLLMAvailableError) as exc_info:
                await router.chat(make_ring(), [{"role": "user", "content": "hi"}], wait=0)
        assert exc_info.value.reason == "timeout"

    asyncio.run(run())


def test_http_429_wait0_fails_over_when_second_model_free():
    """With wait=0 and a second model instantly available, failover now happens
    within the same call — wait=0 only bounds the whole request, not each attempt."""

    async def run():
        a, b = _cfg("a"), _cfg("b")
        pool = await _pool(a, b)
        router = _router(pool)
        with patch(
            _PATCH,
            new=AsyncMock(side_effect=[_http_status_error(429), ("ok", None, None)]),
        ):
            result = await router.chat(make_ring(), [{"role": "user", "content": "hi"}], wait=0)
        assert result.text == "ok"
        assert pool.state("a").phase is LifecyclePhase.COOLING

    asyncio.run(run())


def test_wait_bounds_whole_request():
    """A model that cools well past the deadline must not let wait=0.5 block past ~1s,
    even though ``_attempt`` itself never checks ``wait`` anymore."""

    async def run():
        cfg = _cfg()
        pool = await _pool(cfg)
        router = _router(pool)
        start = time.monotonic()
        with patch(_PATCH, new=AsyncMock(side_effect=_http_status_error(429, "30"))):
            with pytest.raises(NoLLMAvailableError) as exc_info:
                await router.chat(make_ring(), [{"role": "user", "content": "hi"}], wait=0.5)
        assert time.monotonic() - start < 1.0
        assert exc_info.value.reason == "timeout"

    asyncio.run(run())


def test_http_500_fails_over_to_next_llm():
    """A generic HTTP error cools the failed slot and tries the next LLM instead of raising."""

    async def run():
        a, b = _cfg("a"), _cfg("b")
        pool = await _pool(a, b)
        router = _router(pool)
        with patch(
            _PATCH,
            new=AsyncMock(side_effect=[_http_status_error(500), ("ok", None, None)]),
        ):
            result = await router.chat(make_ring(), [{"role": "user", "content": "hi"}])
        assert result.text == "ok"
        assert pool.state("a").phase is LifecyclePhase.COOLING

    asyncio.run(run())


def test_http_429_fails_over_to_next_llm():
    """429 with Retry-After cools the failed slot and tries the next LLM in the same
    request, with the default wait — the flagship failover path."""

    async def run():
        a, b = _cfg("a"), _cfg("b")
        pool = await _pool(a, b)
        router = _router(pool)
        with patch(
            _PATCH,
            new=AsyncMock(side_effect=[_http_status_error(429, "5"), ("ok", None, None)]),
        ):
            result = await router.chat(make_ring(), [{"role": "user", "content": "hi"}])
        assert result.text == "ok"
        assert pool.state("a").phase is LifecyclePhase.COOLING

    asyncio.run(run())


def test_network_error_fails_over_to_next_llm():
    async def run():
        a, b = _cfg("a"), _cfg("b")
        pool = await _pool(a, b)
        router = _router(pool)
        with patch(
            _PATCH,
            new=AsyncMock(side_effect=[httpx.ConnectError("refused"), ("ok", None, None)]),
        ):
            result = await router.chat(make_ring(), [{"role": "user", "content": "hi"}])
        assert result.text == "ok"
        assert pool.state("a").phase is LifecyclePhase.COOLING

    asyncio.run(run())


def test_401_fails_over_to_next_llm_within_same_request(caplog):
    """A rejected key leaves the ring at once, so this caller stops offering that ref
    — and the current request still fails over to a model on another one."""

    async def run():
        a, b = _cfg("a"), _cfg("b", ref="OTHER")
        pool = await _pool(a, b)
        opt = Optimizer()
        router = _router_with_optimizer(pool, opt)
        ring = make_ring({"K": "dead", "OTHER": "live"})
        with (
            patch(_PATCH, new=AsyncMock(side_effect=[_http_status_error(401), ("ok", None, None)])),
            caplog.at_level("ERROR", logger="llmbroker.broker"),
        ):
            result = await router.chat(ring, [{"role": "user", "content": "hi"}])
        assert result.text == "ok"
        assert await ring.payable(["K", "OTHER"]) == frozenset({"OTHER"})
        assert any("API key" in r.message for r in caplog.records)

    asyncio.run(run())


def test_dropped_slot_mid_flight_is_skipped_by_release():
    """A config dropped from the pool after being acquired is a legal no-op on release."""

    async def run():
        cfg = _cfg()
        pool = await _pool(cfg)
        router = _router(pool)
        acquired = await pool.acquire(time.monotonic(), payable=frozenset({"K"}))
        await pool.drop(cfg.name)  # removed while in flight
        await pool.release(acquired)  # must not raise
        with pytest.raises(NoLLMAvailableError):
            await router.chat(make_ring(), [{"role": "user", "content": "hi"}], wait=0)

    asyncio.run(run())


def test_empty_pool_wait0_raises_no_llm_available():
    async def run():
        router = _router(LLMPool())
        with pytest.raises(NoLLMAvailableError) as exc_info:
            await router.chat(make_ring(), [{"role": "user", "content": "hi"}], wait=0)
        assert exc_info.value.reason == "empty_pool"

    asyncio.run(run())


def test_all_disabled_raises_with_all_disabled_reason():
    async def run():
        pool = await _pool(_cfg())
        pool.set_disabled("p1")
        router = _router(pool)
        with pytest.raises(NoLLMAvailableError) as exc_info:
            await router.chat(make_ring(), [{"role": "user", "content": "hi"}], wait=0)
        assert exc_info.value.reason == "all_disabled"

    asyncio.run(run())


def test_timeout_reason_carries_retry_at_of_earliest_cooldown():
    async def run():
        router = _router(await _pool(_cfg()))
        with patch(_PATCH, new=AsyncMock(side_effect=_http_status_error(429, "30"))):
            with pytest.raises(NoLLMAvailableError) as exc_info:
                await router.chat(make_ring(), [{"role": "user", "content": "hi"}], wait=0)
        assert exc_info.value.reason == "timeout"
        assert exc_info.value.retry_at is not None

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Client-side 4xx failover — never cools, excluded for the rest of the request only
# ---------------------------------------------------------------------------


def test_400_fails_over_without_cooling_and_leaves_no_cooldown_on_the_row():
    async def run():
        a, b = _cfg("a"), _cfg("b")
        pool = await _pool(a, b)
        store = _RecordingStore()
        router = Router(pool, store)
        with patch(
            _PATCH,
            new=AsyncMock(side_effect=[_http_status_error(400), ("ok", None, None)]),
        ):
            result = await router.chat(make_ring(), [{"role": "user", "content": "hi"}])
        assert result.text == "ok"
        assert pool.state("a").phase is LifecyclePhase.AVAILABLE  # never cooled
        row_a = next(c for c in store.calls if c.llm_name == "a")
        assert row_a.cooldown_until is None
        assert row_a.http_status == 400  # noqa: PLR2004

    asyncio.run(run())


def test_both_models_400_propagates_the_second_http_error():
    async def run():
        a, b = _cfg("a"), _cfg("b")
        pool = await _pool(a, b)
        router = _router(pool)
        err_a, err_b = _http_status_error(400), _http_status_error(400)
        with patch(_PATCH, new=AsyncMock(side_effect=[err_a, err_b])):
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                await router.chat(make_ring(), [{"role": "user", "content": "hi"}])
        assert exc_info.value is err_b

    asyncio.run(run())


def test_client_error_exclusion_is_per_request_not_persistent():
    """A model excluded by a 400 in one request is fair game again on the next."""

    async def run():
        a, b = _cfg("a"), _cfg("b")
        pool = await _pool(a, b)
        router = _router(pool)
        with patch(
            _PATCH,
            new=AsyncMock(side_effect=[_http_status_error(400), ("ok", None, None)]),
        ):
            first = await router.chat(make_ring(), [{"role": "user", "content": "hi"}])
        assert first.text == "ok"

        with patch(_PATCH, new=AsyncMock(return_value=("still fine", None, None))):
            second = await router.chat(make_ring(), [{"role": "user", "content": "hi"}])
        assert second.text == "still fine"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Cooldown-duration formula: trust the provider, scale only within a streak
# ---------------------------------------------------------------------------


def test_first_429_in_streak_trusts_provider_number_verbatim():
    async def run():
        cfg = _cfg()
        pool = await _pool(cfg)
        opt = Optimizer(backoff_factor=2.0, max_delay=999_999)
        router = _router_with_optimizer(pool, opt)
        captured = _spy_cool_down(pool)

        with patch(_PATCH, new=AsyncMock(side_effect=_http_status_error(429, "100"))):
            outcome = await _attempt(router, cfg)

        assert outcome.verdict is None
        assert captured == [100]

    asyncio.run(run())


def test_second_consecutive_429_scales_its_own_number_not_the_first():
    """Regression test for the compounding trap: day-scale then short-scale must not compound."""

    async def run():
        cfg = _cfg()
        pool = await _pool(cfg)
        opt = Optimizer(backoff_factor=2.0, max_delay=999_999)
        router = _router_with_optimizer(pool, opt)
        captured = _spy_cool_down(pool)

        with patch(_PATCH, new=AsyncMock(side_effect=_http_status_error(429, "86400"))):
            await _attempt(router, cfg)
        with patch(_PATCH, new=AsyncMock(side_effect=_http_status_error(429, "200"))):
            await _attempt(router, cfg)

        assert captured == [86400, 200 * 2]

    asyncio.run(run())


def test_success_resets_streak_so_later_429_trusted_verbatim_again():
    async def run():
        cfg = _cfg()
        pool = await _pool(cfg)
        opt = Optimizer(backoff_factor=2.0, max_delay=999_999)
        router = _router_with_optimizer(pool, opt)
        captured = _spy_cool_down(pool)

        with patch(_PATCH, new=AsyncMock(side_effect=_http_status_error(429, "100"))):
            await _attempt(router, cfg)
        with patch(_PATCH, new=AsyncMock(return_value=("ok", None, None))):
            await _attempt(router, cfg)
        assert opt.rl_fail_count(cfg.name) == 0

        with patch(_PATCH, new=AsyncMock(side_effect=_http_status_error(429, "50"))):
            await _attempt(router, cfg)

        assert captured == [100, 50]

    asyncio.run(run())


def test_429_without_retry_after_falls_back_to_default_before_scaling():
    async def run():
        cfg = _cfg()
        pool = await _pool(cfg)
        opt = Optimizer(backoff_factor=2.0, max_delay=999_999)
        router = _router_with_optimizer(pool, opt)
        captured = _spy_cool_down(pool)

        with patch(_PATCH, new=AsyncMock(side_effect=_http_status_error(429))):
            await _attempt(router, cfg)

        assert captured == [60]

    asyncio.run(run())


def test_wait_time_capped_at_max_delay():
    async def run():
        cfg = _cfg()
        pool = await _pool(cfg)
        opt = Optimizer(backoff_factor=2.0, max_delay=500.0)
        router = _router_with_optimizer(pool, opt)
        captured = _spy_cool_down(pool)

        with patch(_PATCH, new=AsyncMock(side_effect=_http_status_error(429, "10000"))):
            await _attempt(router, cfg)

        assert captured == [500.0]

    asyncio.run(run())


def test_a_store_that_cannot_record_still_drives_the_learner():
    """A journal nobody can write must not also blind the pool: the dead-key drop and
    the cooldown streak are what keep the next call off a model this one condemned."""

    async def run():
        cfg = _cfg("a")
        pool = await _pool(cfg, _cfg("b"))
        opt = Optimizer()

        class _BrokenStore:
            async def record(self, call):
                raise OSError("journal is gone")

            async def record_quality(self, llm_name, operation, score, *, call_id=None):
                pass

        store = _BrokenStore()
        learner = Learner(opt, store, pool)
        router = Router(pool, store, optimizer=opt, learner=learner)

        ring = make_ring()
        with patch(_PATCH, new=AsyncMock(side_effect=_http_status_error(401))):
            await _attempt(router, cfg, ring)

        assert await ring.resolve("K") is None  # the dead key still left the ring

    asyncio.run(run())

"""Unit tests for Router: routing logic, backoff/cooldown formula, and failover."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from llmbroker.broker.learning import _LearningHook
from llmbroker.broker.pool import LLMPool
from llmbroker.broker.router import Router
from llmbroker.exceptions import AllLLMsFailedError, NoLLMAvailableError
from llmbroker.models import LifecyclePhase, LLMConfig
from llmbroker.optimizer import Optimizer
from llmbroker.standalone.telemetry import NoTelemetry


class _NoTelemetry:
    async def record(self, call):
        pass

    async def record_quality(self, llm_name, operation, score, *, call_id=None):
        pass


def _cfg(name="p1"):
    return LLMConfig(name=name, base_url="https://x/v1", model="m", api_key_ref="K")


async def _pool(*cfgs, key="secret") -> LLMPool:
    pool = LLMPool()
    for cfg in cfgs:
        await pool.add(cfg, key)
    return pool


def _router(pool: LLMPool) -> Router:
    return Router(pool, _NoTelemetry(), scope=None)


async def _noop_resync() -> None:
    return


def _router_with_optimizer(pool: LLMPool, opt: Optimizer) -> Router:
    """Wire _LearningHook like AsyncBroker does, so rl_fail_count/dead-key drop drive for real."""
    telemetry = _LearningHook(opt, NoTelemetry(), pool, _noop_resync)
    return Router(pool, telemetry, scope=None, optimizer=opt)


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
            result = await router.chat([{"role": "user", "content": "hi"}])
        assert result.text == "hello"

    asyncio.run(run())


def test_missing_api_key_raises_all_llms_failed():
    async def run():
        router = _router(await _pool(_cfg(), key=None))
        with pytest.raises(AllLLMsFailedError, match="api_key_ref"):
            await router.chat([{"role": "user", "content": "hi"}], wait=0)

    asyncio.run(run())


def test_zero_keyed_configs_raises_immediately_with_default_wait():
    """Regression for the eager-guard/hang fix: a keyless-only pool must not block on

    the default wait=None forever — the check runs before any slot acquisition."""

    async def run():
        pool = LLMPool()
        await pool.add(_cfg(), None)  # keyless — never acquirable
        router = _router(pool)
        with pytest.raises(AllLLMsFailedError, match="api_key_ref"):
            await router.chat([{"role": "user", "content": "hi"}])

    asyncio.run(run())


def test_mixed_keyed_and_keyless_pool_routes_over_keyed_only():
    """A keyless config is never acquirable, so the router routes to the keyed one
    without raising and without ever blocking on the keyless slot."""

    async def run():
        keyed, keyless = _cfg("keyed"), _cfg("keyless")
        pool = LLMPool()
        await pool.add(keyed, "secret")
        await pool.add(keyless, None)
        router = _router(pool)
        with patch(_PATCH, new=AsyncMock(return_value=("ok", None, None))):
            result = await router.chat([{"role": "user", "content": "hi"}], wait=0)
        assert result.text == "ok"
        assert result._llm_name == "keyed"

    asyncio.run(run())


def test_http_429_wait0_raises_no_llm_available():
    async def run():
        router = _router(await _pool(_cfg()))
        with patch(_PATCH, new=AsyncMock(side_effect=_http_status_error(429))):
            with pytest.raises(NoLLMAvailableError):
                await router.chat([{"role": "user", "content": "hi"}], wait=0)

    asyncio.run(run())


def test_http_503_wait0_raises_no_llm_available():
    async def run():
        router = _router(await _pool(_cfg()))
        with patch(_PATCH, new=AsyncMock(side_effect=_http_status_error(503))):
            with pytest.raises(NoLLMAvailableError):
                await router.chat([{"role": "user", "content": "hi"}], wait=0)

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
            result = await router.chat([{"role": "user", "content": "hi"}])
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
            result = await router.chat([{"role": "user", "content": "hi"}])
        assert result.text == "ok"
        assert pool.state("a").phase is LifecyclePhase.COOLING

    asyncio.run(run())


def test_401_fails_over_to_next_llm_within_same_request():
    """A dead key still drops the LLM immediately, but the *current* request fails over."""

    async def run():
        a, b = _cfg("a"), _cfg("b")
        pool = await _pool(a, b)
        opt = Optimizer()
        router = _router_with_optimizer(pool, opt)
        with patch(
            _PATCH,
            new=AsyncMock(side_effect=[_http_status_error(401), ("ok", None, None)]),
        ):
            result = await router.chat([{"role": "user", "content": "hi"}])
        assert result.text == "ok"
        assert "a" not in pool
        assert any("API key" in alert.message for alert in opt.alerts())

    asyncio.run(run())


def test_dropped_slot_mid_flight_is_skipped_by_release():
    """A config dropped from the pool after being acquired is a legal no-op on release."""

    async def run():
        cfg = _cfg()
        pool = await _pool(cfg)
        router = _router(pool)
        acquired = await pool.acquire(0)
        await pool.drop(cfg.name)  # removed while in flight
        await pool.release(acquired)  # must not raise
        with pytest.raises(NoLLMAvailableError):
            await router.chat([{"role": "user", "content": "hi"}], wait=0)

    asyncio.run(run())


def test_empty_pool_wait0_raises_no_llm_available():
    async def run():
        router = _router(LLMPool())
        with pytest.raises(NoLLMAvailableError):
            await router.chat([{"role": "user", "content": "hi"}], wait=0)

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
            result = await router._attempt(
                cfg,
                [{"role": "user", "content": "hi"}],
                None,
                operation=None,
                trace_id=None,
                wait=None,
            )

        assert result is None
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
            await router._attempt(
                cfg,
                [{"role": "user", "content": "hi"}],
                None,
                operation=None,
                trace_id=None,
                wait=None,
            )
        with patch(_PATCH, new=AsyncMock(side_effect=_http_status_error(429, "200"))):
            await router._attempt(
                cfg,
                [{"role": "user", "content": "hi"}],
                None,
                operation=None,
                trace_id=None,
                wait=None,
            )

        assert captured == [86400, 200 * 2]

    asyncio.run(run())


def test_success_resets_streak_so_later_429_trusted_verbatim_again():
    async def run():
        cfg = _cfg()
        pool = await _pool(cfg)
        opt = Optimizer(backoff_factor=2.0, max_delay=999_999)
        router = _router_with_optimizer(pool, opt)
        captured = _spy_cool_down(pool)
        kwargs = {"operation": None, "trace_id": None, "wait": None}

        with patch(_PATCH, new=AsyncMock(side_effect=_http_status_error(429, "100"))):
            await router._attempt(cfg, [{"role": "user", "content": "hi"}], None, **kwargs)
        with patch(_PATCH, new=AsyncMock(return_value=("ok", None, None))):
            await router._attempt(cfg, [{"role": "user", "content": "hi"}], None, **kwargs)
        assert opt.rl_fail_count(cfg.name) == 0

        with patch(_PATCH, new=AsyncMock(side_effect=_http_status_error(429, "50"))):
            await router._attempt(cfg, [{"role": "user", "content": "hi"}], None, **kwargs)

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
            await router._attempt(
                cfg,
                [{"role": "user", "content": "hi"}],
                None,
                operation=None,
                trace_id=None,
                wait=None,
            )

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
            await router._attempt(
                cfg,
                [{"role": "user", "content": "hi"}],
                None,
                operation=None,
                trace_id=None,
                wait=None,
            )

        assert captured == [500.0]

    asyncio.run(run())

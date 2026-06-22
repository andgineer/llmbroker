"""Unit tests for Router: routing logic, error escalation, and slot handling."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from llmbroker.broker.pool import LLMPool
from llmbroker.broker.router import Router
from llmbroker.exceptions import AllLLMsFailedError, NoLLMAvailableError
from llmbroker.models import LLMConfig


class _NoTelemetry:
    async def record(self, call):
        pass

    async def record_quality(self, call_id, score):
        raise KeyError(call_id)


def _cfg(name="p1"):
    return LLMConfig(name=name, base_url="https://x/v1", model="m", api_key_ref="K")


def _pool(*cfgs, key="secret") -> LLMPool:
    pool = LLMPool(state_store=None, user_id=None)
    for cfg in cfgs:
        pool.add(cfg, key)
    return pool


def _router(pool: LLMPool) -> Router:
    return Router(pool, _NoTelemetry(), user_id=None)


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {}
    resp.text = f"HTTP {status}"
    return httpx.HTTPStatusError("err", request=MagicMock(), response=resp)


_PATCH = "llmbroker.broker.router.call_provider"


def test_happy_path_returns_result():
    async def run():
        router = _router(_pool(_cfg()))
        with patch(_PATCH, new=AsyncMock(return_value=("hello", None, None))):
            result = await router.chat([{"role": "user", "content": "hi"}])
        assert result.text == "hello"

    asyncio.run(run())


def test_missing_api_key_raises_all_llms_failed():
    async def run():
        router = _router(_pool(_cfg(), key=None))
        with pytest.raises(AllLLMsFailedError, match="api_key_ref"):
            await router.chat([{"role": "user", "content": "hi"}], wait=0)

    asyncio.run(run())


def test_http_429_wait0_raises_no_llm_available():
    async def run():
        router = _router(_pool(_cfg()))
        with patch(_PATCH, new=AsyncMock(side_effect=_http_status_error(429))):
            with pytest.raises(NoLLMAvailableError):
                await router.chat([{"role": "user", "content": "hi"}], wait=0)

    asyncio.run(run())


def test_http_503_wait0_raises_no_llm_available():
    async def run():
        router = _router(_pool(_cfg()))
        with patch(_PATCH, new=AsyncMock(side_effect=_http_status_error(503))):
            with pytest.raises(NoLLMAvailableError):
                await router.chat([{"role": "user", "content": "hi"}], wait=0)

    asyncio.run(run())


def test_http_500_raises_all_llms_failed():
    async def run():
        router = _router(_pool(_cfg()))
        with patch(_PATCH, new=AsyncMock(side_effect=_http_status_error(500))):
            with pytest.raises(AllLLMsFailedError):
                await router.chat([{"role": "user", "content": "hi"}])

    asyncio.run(run())


def test_network_error_raises_all_llms_failed():
    async def run():
        router = _router(_pool(_cfg()))
        with patch(_PATCH, new=AsyncMock(side_effect=httpx.ConnectError("refused"))):
            with pytest.raises(AllLLMsFailedError):
                await router.chat([{"role": "user", "content": "hi"}])

    asyncio.run(run())


def test_stale_slot_is_skipped():
    """A config dropped from the pool while already enqueued is silently skipped."""

    async def run():
        cfg = _cfg()
        pool = _pool(cfg)
        pool.drop(cfg.name)  # stale: slot in queue, but name not in pool
        router = _router(pool)
        with pytest.raises(NoLLMAvailableError):
            await router.chat([{"role": "user", "content": "hi"}], wait=0)

    asyncio.run(run())


def test_empty_pool_wait0_raises_no_llm_available():
    async def run():
        router = _router(LLMPool(state_store=None, user_id=None))
        with pytest.raises(NoLLMAvailableError):
            await router.chat([{"role": "user", "content": "hi"}], wait=0)

    asyncio.run(run())

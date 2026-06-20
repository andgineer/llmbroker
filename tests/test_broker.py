"""Tests for AsyncBroker core routing, add/remove, error escalation."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from llmbroker.broker import AllLLMsFailedError, AsyncBroker, NoLLMAvailableError
from llmbroker.models import LLMConfig
from llmbroker.registry import Registry as FileRegistry
from llmbroker.telemetry import NoTelemetry


def _registry(tmp_path, entries=None):
    if entries is None:
        entries = [("p1", "https://x/v1", "m", "K")]
    lines = []
    for name, base_url, model, ref in entries:
        lines += [
            "[[llms]]",
            f'name="{name}"',
            f'base_url="{base_url}"',
            f'model="{model}"',
            f'api_key_ref="{ref}"',
        ]
    f = tmp_path / "llms.toml"
    f.write_text("\n".join(lines) + "\n")
    return FileRegistry(f)


def _http_ok(content="hello"):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"choices": [{"message": {"role": "assistant", "content": content}}]}
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=cm)
    cm.__aexit__ = AsyncMock(return_value=False)
    cm.post = AsyncMock(return_value=resp)
    return cm


def _http_error(status):
    mock_request = MagicMock()
    mock_response = MagicMock()
    mock_response.status_code = status
    mock_response.headers = {}
    mock_response.text = f"HTTP {status}"
    exc = httpx.HTTPStatusError("err", request=mock_request, response=mock_response)
    resp = MagicMock()
    resp.raise_for_status.side_effect = exc
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=cm)
    cm.__aexit__ = AsyncMock(return_value=False)
    cm.post = AsyncMock(return_value=resp)
    return cm


def test_ensure_started_populates_configs(tmp_path):
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
            await broker.ensure_started()
            assert "p1" in broker
            assert len(broker) == 1

    asyncio.run(run())


def test_ensure_started_idempotent(tmp_path):
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
            await broker.ensure_started()
            await broker.ensure_started()
            assert len(broker) == 1

    asyncio.run(run())


def test_iter_yields_names(tmp_path):
    async def run():
        entries = [("a", "https://a/v1", "m", "K"), ("b", "https://b/v1", "m", "K")]
        async with AsyncBroker(
            registry=_registry(tmp_path, entries), telemetry=NoTelemetry()
        ) as broker:
            await broker.ensure_started()
            assert set(broker) == {"a", "b"}

    asyncio.run(run())


def test_getitem_returns_async_llm_with_correct_config(tmp_path):
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
            await broker.ensure_started()
            llm = broker["p1"]
            assert llm.config.name == "p1"
            assert llm.config.base_url == "https://x/v1"

    asyncio.run(run())


def test_getitem_missing_raises_key_error(tmp_path):
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
            await broker.ensure_started()
            with pytest.raises(KeyError):
                _ = broker["nope"]

    asyncio.run(run())


def test_async_llm_state_available(tmp_path):
    async def run():
        from llmbroker.models import LifecyclePhase

        async with AsyncBroker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
            await broker.ensure_started()
            state = await broker["p1"].state()
            assert state.phase is LifecyclePhase.AVAILABLE

    asyncio.run(run())


def test_async_llm_metrics_no_queryable_telemetry(tmp_path):
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
            await broker.ensure_started()
            metrics = await broker["p1"].metrics()
            assert metrics.call_count == 0

    asyncio.run(run())


def test_chat_happy_path(tmp_path):
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
            with patch("llmbroker.broker.httpx.AsyncClient", return_value=_http_ok("world")):
                result = await broker.chat([{"role": "user", "content": "hi"}])
                assert result.text == "world"

    asyncio.run(run())


def test_ask_delegates_to_chat(tmp_path):
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
            with patch("llmbroker.broker.httpx.AsyncClient", return_value=_http_ok("yes")):
                result = await broker.ask("prompt")
                assert result.text == "yes"

    asyncio.run(run())


def test_chat_429_wait0_raises_no_llm_available(tmp_path):
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
            with patch("llmbroker.broker.httpx.AsyncClient", return_value=_http_error(429)):
                with pytest.raises(NoLLMAvailableError):
                    await broker.chat([{"role": "user", "content": "hi"}], wait=0)

    asyncio.run(run())


def test_chat_500_raises_all_llms_failed(tmp_path):
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
            with patch("llmbroker.broker.httpx.AsyncClient", return_value=_http_error(500)):
                with pytest.raises(AllLLMsFailedError):
                    await broker.chat([{"role": "user", "content": "hi"}])

    asyncio.run(run())


def test_chat_empty_pool_wait0_raises_no_llm_available(tmp_path):
    async def run():
        f = tmp_path / "empty.toml"
        f.write_text("")
        async with AsyncBroker(registry=FileRegistry(f), telemetry=NoTelemetry()) as broker:
            with pytest.raises(NoLLMAvailableError):
                await broker.chat([{"role": "user", "content": "hi"}], wait=0)

    asyncio.run(run())


def test_result_record_quality_does_not_raise(tmp_path):
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
            with patch("llmbroker.broker.httpx.AsyncClient", return_value=_http_ok("hi")):
                result = await broker.chat([{"role": "user", "content": "x"}])
                await result.record_quality(1.0)

    asyncio.run(run())


def test_add_with_readonly_registry_raises(tmp_path):
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
            await broker.ensure_started()
            with pytest.raises(TypeError, match="read-only"):
                await broker.add(LLMConfig(name="p2", base_url="u", model="m", api_key_ref="K"))

    asyncio.run(run())


def test_sync_configs_with_readonly_registry_raises(tmp_path, real_broker_sync):  # noqa: ARG001
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
            with pytest.raises(TypeError, match="read-only"):
                await broker.sync_configs(FileRegistry(tmp_path / "other.toml"))

    asyncio.run(run())


def test_calls_without_queryable_telemetry_raises(tmp_path):
    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
            with pytest.raises(TypeError, match="queryable"):
                await broker.calls(limit=10)

    asyncio.run(run())


def test_purge_calls_without_queryable_telemetry_raises(tmp_path):
    from datetime import UTC, datetime

    async def run():
        async with AsyncBroker(registry=_registry(tmp_path), telemetry=NoTelemetry()) as broker:
            with pytest.raises(TypeError, match="queryable"):
                await broker.purge_calls(before=datetime.now(UTC))

    asyncio.run(run())


def test_snapshot_returns_entry_per_llm(tmp_path):
    async def run():
        entries = [("a", "https://a/v1", "m", "K"), ("b", "https://b/v1", "m", "K")]
        async with AsyncBroker(
            registry=_registry(tmp_path, entries), telemetry=NoTelemetry()
        ) as broker:
            snap = await broker.snapshot()
            assert set(snap) == {"a", "b"}
            assert snap["a"].config.name == "a"

    asyncio.run(run())

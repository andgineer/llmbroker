"""Real-transport tests (mission #6/#8, step 6 shared client): exercise the actual
httpx request/response path via ``httpx.MockTransport`` instead of mocking
``call_provider`` directly — headers, JSON parsing, and client reuse all matter here.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import patch

import httpx
import pytest

from llmbroker.broker.broker import AsyncBroker
from llmbroker.exceptions import NoLLMAvailableError
from llmbroker.standalone.registry import Registry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import FileStore


def _registry(tmp_path, name="p1"):
    f = tmp_path / "llms.toml"
    f.write_text(f'[[llms]]\nname="{name}"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n')
    return Registry(f)


def _broker(tmp_path) -> AsyncBroker:
    return AsyncBroker(
        registry=_registry(tmp_path),
        secrets=DictSecrets({"K": "test"}),
        store=FileStore(tmp_path / "store"),
    )


def _ok_body(content="hi", tool_calls=None, usage=None) -> dict:
    message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    body: dict = {"choices": [{"message": message}]}
    if usage is not None:
        body["usage"] = usage
    return body


def test_tool_calls_round_trip_through_broker_chat(tmp_path):
    async def run():
        tool_calls = [
            {"id": "call_1", "type": "function", "function": {"name": "foo", "arguments": "{}"}},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_ok_body(content=None, tool_calls=tool_calls))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=1.0)
        async with _broker(tmp_path) as broker:
            with patch("llmbroker.chat.make_client", return_value=client):
                result = await broker.chat(
                    [{"role": "user", "content": "hi"}],
                    tools=[{"type": "function", "function": {"name": "foo"}}],
                )
        assert result.tool_calls == tool_calls

    asyncio.run(run())


def test_usage_extra_lands_in_usage_extra(tmp_path):
    async def run():
        usage = {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3, "cached_tokens": 5}

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_ok_body(usage=usage))

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=1.0)
        async with _broker(tmp_path) as broker:
            with patch("llmbroker.chat.make_client", return_value=client):
                result = await broker.ask("hi")
        assert result.usage.extra == {"cached_tokens": 5}

    asyncio.run(run())


def test_retry_after_http_date_cooldown_within_tolerance(tmp_path):
    async def run():
        retry_at = datetime.now(UTC) + timedelta(seconds=30)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429,
                headers={"Retry-After": format_datetime(retry_at, usegmt=True)},
                json={"error": "rate limited"},
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=1.0)
        async with _broker(tmp_path) as broker:
            with patch("llmbroker.chat.make_client", return_value=client):
                with pytest.raises(NoLLMAvailableError):
                    await broker.chat([{"role": "user", "content": "hi"}], wait=0)
            rows = await broker.calls(limit=10)

        row = next(c for c in rows if c.llm_name == "p1")
        assert row.cooldown_until is not None
        assert abs((row.cooldown_until - retry_at).total_seconds()) < 2

    asyncio.run(run())


def test_connection_reuse_make_client_called_once_across_two_asks(tmp_path):
    async def run():
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_ok_body())

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=1.0)
        async with _broker(tmp_path) as broker:
            with patch("llmbroker.chat.make_client", return_value=client) as mock_make_client:
                await broker.ask("one")
                await broker.ask("two")
                assert mock_make_client.call_count == 1

    asyncio.run(run())

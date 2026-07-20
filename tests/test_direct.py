"""Tests for the direct single-model client — mocked httpx transport, no network."""

import asyncio

import httpx
import pytest

from llmbroker.direct import AsyncDirectClient, DirectClient, DirectResult
from llmbroker.exceptions import (
    AuthError,
    LLMRequestError,
    LLMTimeoutError,
    ProviderError,
    RateLimitError,
)


def _ok_body(content="hi", usage=None) -> dict:
    body: dict = {"choices": [{"message": {"role": "assistant", "content": content}}]}
    if usage is not None:
        body["usage"] = usage
    return body


def _async_client(handler, **kwargs) -> AsyncDirectClient:
    transport = httpx.MockTransport(handler)
    return AsyncDirectClient(
        base_url="https://api.example.com/v1",
        model="m",
        api_key="k",
        client=httpx.AsyncClient(transport=transport, timeout=1.0),
        **kwargs,
    )


def _sync_client(handler, **kwargs) -> DirectClient:
    transport = httpx.MockTransport(handler)
    return DirectClient(
        base_url="https://api.example.com/v1",
        model="m",
        api_key="k",
        client=httpx.Client(transport=transport, timeout=1.0),
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# async ask
# --------------------------------------------------------------------------- #


def test_async_ask_returns_text_and_usage():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body("hello", usage={"total_tokens": 7}))

    async def run():
        client = _async_client(handler)
        result = await client.ask("hi")
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert isinstance(result, DirectResult)
    assert result.text == "hello"
    assert result.usage is not None
    assert result.usage.total_tokens == 7


def test_async_ask_sends_correct_request():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        return httpx.Response(200, json=_ok_body())

    async def run():
        client = _async_client(handler)
        await client.ask("hi")
        await client.aclose()

    asyncio.run(run())
    assert seen["url"] == "https://api.example.com/v1/chat/completions"
    assert seen["auth"] == "Bearer k"


def test_async_ask_401_raises_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    async def run():
        client = _async_client(handler)
        with pytest.raises(AuthError) as exc_info:
            await client.ask("hi")
        await client.aclose()
        return exc_info.value

    err = asyncio.run(run())
    assert err.status == 401
    assert "bad key" in (err.detail or "")


def test_async_ask_429_raises_rate_limit_with_retry_after():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "12"}, json={"error": "slow down"})

    async def run():
        client = _async_client(handler)
        with pytest.raises(RateLimitError) as exc_info:
            await client.ask("hi")
        await client.aclose()
        return exc_info.value

    err = asyncio.run(run())
    assert err.status == 429
    assert err.retry_after == 12


def test_async_ask_500_raises_provider_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    async def run():
        client = _async_client(handler)
        with pytest.raises(ProviderError) as exc_info:
            await client.ask("hi")
        await client.aclose()
        return exc_info.value

    err = asyncio.run(run())
    assert type(err) is ProviderError
    assert err.status == 500


def test_async_ask_timeout_raises_llm_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async def run():
        client = _async_client(handler)
        with pytest.raises(LLMTimeoutError):
            await client.ask("hi")
        await client.aclose()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# async stream
# --------------------------------------------------------------------------- #

_SSE = (
    b'data: {"choices": [{"delta": {"content": "Hel"}}]}\n\n'
    b'data: {"choices": [{"delta": {"content": "lo"}}]}\n\n'
    b'data: {"choices": [], "usage": {"total_tokens": 3}}\n\n'
    b"data: [DONE]\n\n"
)


def test_async_stream_yields_deltas():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_SSE, headers={"content-type": "text/event-stream"})

    async def run():
        client = _async_client(handler)
        deltas = [d async for d in client.stream("hi")]
        await client.aclose()
        return deltas

    deltas = asyncio.run(run())
    assert deltas == ["Hel", "lo"]
    assert "".join(deltas) == "Hello"


def test_async_stream_sets_stream_flag():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=b"data: [DONE]\n\n")

    async def run():
        client = _async_client(handler)
        _ = [d async for d in client.stream("hi")]
        await client.aclose()

    asyncio.run(run())
    assert seen["body"]["stream"] is True


def test_async_stream_error_status_raises_before_yield():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden"})

    async def run():
        client = _async_client(handler)
        with pytest.raises(AuthError) as exc_info:
            _ = [d async for d in client.stream("hi")]
        await client.aclose()
        return exc_info.value

    err = asyncio.run(run())
    assert err.status == 403


# --------------------------------------------------------------------------- #
# sync ask
# --------------------------------------------------------------------------- #


def test_sync_ask_returns_text():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body("sync-hi"))

    with _sync_client(handler) as client:
        result = client.ask("hi")
    assert result.text == "sync-hi"


def test_sync_ask_403_raises_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="nope")

    with _sync_client(handler) as client:
        with pytest.raises(AuthError):
            client.ask("hi")


def test_sync_ask_timeout_raises_llm_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    with _sync_client(handler) as client:
        with pytest.raises(LLMTimeoutError):
            client.ask("hi")


# --------------------------------------------------------------------------- #
# input surface + error hierarchy
# --------------------------------------------------------------------------- #


def test_messages_passthrough():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_body())

    async def run():
        client = _async_client(handler)
        await client.ask(messages=[{"role": "system", "content": "s"}])
        await client.aclose()

    asyncio.run(run())
    assert seen["body"]["messages"] == [{"role": "system", "content": "s"}]


def test_prompt_and_messages_both_is_error():
    async def run():
        client = _async_client(lambda request: httpx.Response(200, json=_ok_body()))
        with pytest.raises(ValueError, match="exactly one"):
            await client.ask("hi", messages=[{"role": "user", "content": "x"}])
        await client.aclose()

    asyncio.run(run())


def test_neither_prompt_nor_messages_is_error():
    async def run():
        client = _async_client(lambda request: httpx.Response(200, json=_ok_body()))
        with pytest.raises(ValueError, match="exactly one"):
            await client.ask()
        await client.aclose()

    asyncio.run(run())


def test_error_hierarchy():
    assert issubclass(AuthError, ProviderError)
    assert issubclass(RateLimitError, ProviderError)
    assert issubclass(ProviderError, LLMRequestError)
    assert issubclass(LLMTimeoutError, LLMRequestError)


def test_external_client_not_closed_on_aclose():
    external = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=_ok_body())),
        timeout=1.0,
    )

    async def run():
        client = AsyncDirectClient(base_url="https://x/v1", model="m", api_key="k", client=external)
        await client.ask("hi")
        await client.aclose()
        closed = external.is_closed
        await external.aclose()
        return closed

    assert asyncio.run(run()) is False

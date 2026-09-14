"""Tests for the direct single-model client — mocked httpx transport, no network."""

import asyncio
import json

import httpx
import pytest

from llmbroker.direct import AsyncDirectClient, DirectClient, DirectResult
from llmbroker.exceptions import (
    AuthError,
    InvalidProviderResponseError,
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


@pytest.mark.parametrize(
    ("response", "snippet"),
    [
        (httpx.Response(200, text="<html>gateway</html>"), "gateway"),
        (httpx.Response(200, json={"error": {"message": "quota exceeded"}}), "quota exceeded"),
    ],
    ids=["invalid_json", "no_choices"],
)
def test_async_ask_garbage_200_raises_a_typed_error(response, snippet):
    """No failover here by design, but the error is still one of ours: a raw
    JSONDecodeError or KeyError out of a public call is not in the contract."""

    async def run():
        client = _async_client(lambda request: response)
        with pytest.raises(InvalidProviderResponseError) as exc_info:
            await client.ask("hi")
        await client.aclose()
        return exc_info.value

    err = asyncio.run(run())
    assert isinstance(err, LLMRequestError)
    assert err.model == "m"
    assert snippet in err.detail


def test_sync_ask_garbage_200_raises_a_typed_error():
    client = _sync_client(lambda request: httpx.Response(200, text="not json at all"))
    with pytest.raises(InvalidProviderResponseError):
        client.ask("hi")
    client.close()


def test_error_status_with_a_non_json_body_still_maps_to_the_status_error():
    """The status wins over the body: an HTML 503 page is a provider error, not an
    unparseable completion."""
    client = _sync_client(lambda request: httpx.Response(503, text="<html>down</html>"))
    with pytest.raises(RateLimitError) as exc_info:
        client.ask("hi")
    client.close()
    assert exc_info.value.status == 503


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
        return httpx.Response(
            200,
            content=b'data: {"choices": [{"delta": {"content": "x"}}]}\n\ndata: [DONE]\n\n',
        )

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


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (b"<html><body>502 from your proxy</body></html>", "text/html"),
        (b'{"choices": [{"message": {"content": "hi"}}]}', "application/json"),
        (
            b'data: {"error": {"message": "upstream rate limit"}}\n\ndata: [DONE]\n\n',
            "text/event-stream",
        ),
    ],
)
def test_async_stream_garbage_200_raises_instead_of_yielding_nothing(body, content_type):
    """A proxy error page, a provider ignoring `stream`, an SSE-framed error payload:
    `ask` raises on all three, so a stream must not hand back a silent empty answer."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": content_type})

    async def run():
        client = _async_client(handler)
        with pytest.raises(InvalidProviderResponseError) as exc_info:
            _ = [d async for d in client.stream("hi")]
        await client.aclose()
        return exc_info.value

    err = asyncio.run(run())
    assert err.model == "m"
    assert "no chat-completion chunks decoded" in (err.detail or "")


def test_async_stream_on_an_empty_completion_raises_rather_than_ending_silently():
    """A well-formed stream carrying no delta is no answer, and direct has nothing to
    fail over to — so it raises, diagnosed apart from a body that is not a stream."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n'
            b"data: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )

    async def run():
        client = _async_client(handler)
        with pytest.raises(InvalidProviderResponseError) as exc_info:
            _ = [d async for d in client.stream("hi")]
        await client.aclose()
        return exc_info.value

    err = asyncio.run(run())
    assert err.model == "m"
    assert "no chat-completion chunks decoded" not in (err.detail or "")


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

    with _sync_client(handler) as client, pytest.raises(AuthError):
        client.ask("hi")


def test_sync_ask_timeout_raises_llm_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    with _sync_client(handler) as client, pytest.raises(LLMTimeoutError):
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


# --------------------------------------------------------------------------- #
# request parameters
# --------------------------------------------------------------------------- #


def test_async_ask_forwards_params():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_body())

    async def run():
        client = _async_client(handler)
        await client.ask("hi", params={"temperature": 0, "reasoning_effort": "low"})
        await client.aclose()

    asyncio.run(run())
    assert seen["body"]["temperature"] == 0
    assert seen["body"]["reasoning_effort"] == "low"


def test_async_stream_forwards_params_and_keeps_streaming_keys():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=_SSE, headers={"content-type": "text/event-stream"})

    async def run():
        client = _async_client(handler)
        _ = [d async for d in client.stream("hi", params={"temperature": 0.5})]
        await client.aclose()

    asyncio.run(run())
    assert seen["body"]["temperature"] == 0.5
    assert seen["body"]["stream"] is True
    assert seen["body"]["stream_options"] == {"include_usage": True}


def test_sync_ask_forwards_params():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_body())

    client = _sync_client(handler)
    client.ask("hi", params={"max_tokens": 16})
    client.close()
    assert seen["body"]["max_tokens"] == 16


def test_reserved_param_raises_before_any_request():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=_ok_body())

    async def run():
        client = _async_client(handler)
        with pytest.raises(ValueError, match="'model'"):
            await client.ask("hi", params={"model": "someone-elses"})
        with pytest.raises(ValueError, match="'stream'"):
            _ = [d async for d in client.stream("hi", params={"stream": False})]
        await client.aclose()

    asyncio.run(run())

    sync = _sync_client(handler)
    with pytest.raises(ValueError, match="'messages'"):
        sync.ask("hi", params={"messages": []})
    sync.close()

    assert calls == []


# --------------------------------------------------------------------------- #
# chat with tools
# --------------------------------------------------------------------------- #

_TOOLS = [
    {
        "type": "function",
        "function": {"name": "add", "parameters": {"type": "object", "properties": {}}},
    },
]
_TOOL_CALLS = [{"id": "c1", "type": "function", "function": {"name": "add", "arguments": "{}"}}]


def _tool_calls_body() -> dict:
    message = {"role": "assistant", "content": None, "tool_calls": _TOOL_CALLS}
    return {"choices": [{"message": message}], "usage": {"total_tokens": 4}}


def test_async_chat_sends_tools_with_tool_choice_and_returns_tool_calls():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_tool_calls_body())

    async def run():
        client = _async_client(handler)
        result = await client.chat([{"role": "user", "content": "1+2?"}], tools=_TOOLS)
        await client.aclose()
        return result

    result = asyncio.run(run())
    assert seen["body"]["tools"] == _TOOLS
    assert seen["body"]["tool_choice"] == "auto"
    assert seen["body"]["messages"] == [{"role": "user", "content": "1+2?"}]
    assert result.tool_calls == _TOOL_CALLS
    assert result.usage.total_tokens == 4


def test_a_tool_calls_only_reply_is_an_answer_not_an_empty_one():
    """No text and tool calls is a reply the caller acts on — only no text and no
    tool calls is the empty answer a direct call raises on."""
    client = _sync_client(lambda request: httpx.Response(200, json=_tool_calls_body()))
    result = client.chat([{"role": "user", "content": "1+2?"}], tools=_TOOLS)
    client.close()
    assert (result.text, result.tool_calls) == ("", _TOOL_CALLS)


def test_sync_chat_sends_tools_and_forwards_params_and_timeout():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["timeout"] = request.extensions["timeout"]
        return httpx.Response(200, json=_ok_body("3"))

    client = _sync_client(handler)
    result = client.chat(
        [{"role": "user", "content": "1+2?"}],
        tools=_TOOLS,
        timeout=0.5,
        params={"tool_choice": "required"},
    )
    client.close()
    assert seen["body"]["tools"] == _TOOLS
    assert seen["body"]["tool_choice"] == "required"
    assert seen["timeout"]["read"] == 0.5
    assert (result.text, result.tool_calls) == ("3", None)


def test_chat_without_tools_sends_no_tool_keys():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_body())

    async def run():
        client = _async_client(handler)
        await client.chat([{"role": "user", "content": "hi"}])
        await client.aclose()

    asyncio.run(run())
    assert "tools" not in seen["body"]
    assert "tool_choice" not in seen["body"]


_NEEDS = {"reasoning_effort": "none"}
_SSE_OK = b'data: {"choices": [{"delta": {"content": "ok"}}]}\n\ndata: [DONE]\n\n'


def _bodies() -> tuple[list[dict], object]:
    """A provider that records every request body and answers each in the shape asked."""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if body.get("stream"):
            return httpx.Response(
                200, content=_SSE_OK, headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(200, json=_ok_body())

    return bodies, handler


def test_async_client_tool_params_ride_a_chat_with_tools_only():
    bodies, handler = _bodies()

    async def run():
        client = _async_client(handler, tool_params=_NEEDS)
        await client.chat([{"role": "user", "content": "1+2?"}], tools=_TOOLS)
        await client.chat([{"role": "user", "content": "hi"}])
        await client.ask("hi")
        assert [delta async for delta in client.stream("hi")] == ["ok"]
        await client.aclose()

    asyncio.run(run())
    assert [b.get("reasoning_effort") for b in bodies] == ["none", None, None, None]
    assert bodies[0]["tools"] == _TOOLS


def test_sync_client_tool_params_ride_a_chat_with_tools_only():
    bodies, handler = _bodies()
    client = _sync_client(handler, tool_params=_NEEDS)
    client.chat([{"role": "user", "content": "1+2?"}], tools=_TOOLS)
    client.chat([{"role": "user", "content": "hi"}])
    client.ask("hi")
    client.close()
    assert [b.get("reasoning_effort") for b in bodies] == ["none", None, None]


def test_a_callers_params_win_over_the_clients_tool_params():
    bodies, handler = _bodies()
    client = _sync_client(handler, tool_params=_NEEDS)
    client.chat([], tools=_TOOLS, params={"reasoning_effort": "low"})
    client.close()

    async def run():
        async_client = _async_client(handler, tool_params=_NEEDS)
        await async_client.chat([], tools=_TOOLS, params={"reasoning_effort": "low"})
        await async_client.aclose()

    asyncio.run(run())
    assert [b["reasoning_effort"] for b in bodies] == ["low", "low"]


def test_chat_refuses_tools_passed_as_a_param():
    client = _sync_client(lambda request: httpx.Response(200, json=_ok_body()))
    with pytest.raises(ValueError, match="'tools'"):
        client.chat([], params={"tools": _TOOLS})
    client.close()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(200, json={"choices": [{"message": {"role": "assistant"}}]}),
    ],
    ids=["provider_error", "empty_answer"],
)
def test_async_chat_raises_like_ask(response):
    async def run():
        client = _async_client(lambda request: response)
        with pytest.raises(LLMRequestError):
            await client.chat([{"role": "user", "content": "hi"}], tools=_TOOLS)
        await client.aclose()

    asyncio.run(run())


def test_async_chat_timeout_raises_llm_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async def run():
        client = _async_client(handler)
        with pytest.raises(LLMTimeoutError):
            await client.chat([{"role": "user", "content": "hi"}], tools=_TOOLS)
        await client.aclose()

    asyncio.run(run())

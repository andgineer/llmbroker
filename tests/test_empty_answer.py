"""A 200 carrying no text and no tool calls is not an answer: pooled it fails over
like any malformed response, direct it raises."""

import asyncio
from contextlib import aclosing

import httpx
import pytest

from llmbroker.broker.pool import LLMPool
from llmbroker.broker.result import CallReceipt
from llmbroker.broker.router import Router
from llmbroker.direct import AsyncDirectClient, DirectClient
from llmbroker.exceptions import InvalidProviderResponseError, NoLLMAvailableError
from llmbroker.models import CallStatus, LifecyclePhase, LLMConfig

from support import make_ring

_TOOLS = [{"type": "function", "function": {"name": "now", "parameters": {}}}]
_TOOL_CALLS = [{"id": "1", "type": "function", "function": {"name": "now", "arguments": "{}"}}]


class _RecordingStore:
    def __init__(self) -> None:
        self.calls: list = []

    async def record(self, call):
        self.calls.append(call)

    async def record_quality(self, call_id, score, *, scope=None):
        pass


def _cfg(name: str) -> LLMConfig:
    return LLMConfig(name=name, base_url=f"https://{name}/v1", model="m", api_key_ref="K")


async def _router(store, *names: str) -> Router:
    pool = LLMPool()
    for name in names:
        await pool.add(_cfg(name))
    return Router(pool, store)


def _mount(router: Router, handler) -> None:
    router._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)  # noqa: SLF001


def _completion(content=None, tool_calls=None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message, "finish_reason": "stop"}]}


def _sse(*deltas: str) -> bytes:
    body = b"".join(
        b'data: {"choices": [{"delta": {"content": "%s"}}]}\n\n' % d.encode() for d in deltas
    )
    return body + b"data: [DONE]\n\n"


_EMPTY_SSE = (
    b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n'
    b'data: {"choices": [], "usage": {"total_tokens": 4}}\n\n'
    b"data: [DONE]\n\n"
)


def _stream_response(content: bytes) -> httpx.Response:
    return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"})


async def _drain(router: Router, **kwargs) -> list[str]:
    routed = router.stream(
        make_ring(),
        [{"role": "user", "content": "hi"}],
        CallReceipt(),
        **kwargs,
    )
    async with aclosing(routed) as deltas:
        return [d async for d in deltas]


# --------------------------------------------------------------------------- #
# completions through the pool
# --------------------------------------------------------------------------- #


def test_an_empty_completion_fails_over_to_the_next_candidate():
    """The measured free-tier failure: a fast, well-shaped 200 saying nothing. The
    caller must get the next candidate's answer, not the silence."""
    store = _RecordingStore()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a":
            return httpx.Response(200, json=_completion(""))
        return httpx.Response(200, json=_completion("ok"))

    async def run():
        router = await _router(store, "a", "b")
        _mount(router, handler)
        return await router.chat(make_ring(), [{"role": "user", "content": "hi"}])

    result = asyncio.run(run())
    assert (result.text, result.llm_name) == ("ok", "b")


def test_an_empty_completion_is_journaled_as_a_failure():
    """A row saying OK would teach the pool that the endpoint answers, and would let a
    host rate an answer that never arrived."""
    store = _RecordingStore()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a":
            return httpx.Response(200, json=_completion(None))
        return httpx.Response(200, json=_completion("ok"))

    async def run():
        router = await _router(store, "a", "b")
        _mount(router, handler)
        await router.chat(make_ring(), [{"role": "user", "content": "hi"}])

    asyncio.run(run())
    assert [(c.llm_name, c.status) for c in store.calls] == [
        ("a", CallStatus.ERROR),
        ("b", CallStatus.OK),
    ]
    assert "no text and no tool calls" in str(store.calls[0].error_detail or "")


def test_a_reply_carrying_only_tool_calls_is_an_answer():
    """Tools are what the model was asked for: a reply that calls one and says nothing
    is complete, and must not be failed over."""
    store = _RecordingStore()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion(None, tool_calls=_TOOL_CALLS))

    async def run():
        router = await _router(store, "a")
        _mount(router, handler)
        return await router.chat(
            make_ring(),
            [{"role": "user", "content": "hi"}],
            tools=_TOOLS,
        )

    result = asyncio.run(run())
    assert (result.text, result.tool_calls) == ("", _TOOL_CALLS)
    assert [(c.llm_name, c.status) for c in store.calls] == [("a", CallStatus.OK)]


def test_the_last_candidate_returning_empty_raises_rather_than_answering():
    """Nothing left to fail over to: the caller gets the exhaustion error, never an
    empty string it would have to check for itself."""
    store = _RecordingStore()

    async def run():
        router = await _router(store, "a")
        _mount(router, lambda _r: httpx.Response(200, json=_completion("")))
        with pytest.raises(NoLLMAvailableError) as exc_info:
            await router.chat(make_ring(), [{"role": "user", "content": "hi"}], wait=0)
        return exc_info.value

    assert asyncio.run(run()).reason == "timeout"


def test_an_empty_answer_cools_the_model_like_any_malformed_response():
    """The provider misbehaved, so it gets the disposal an unusable 200 already has —
    cooled for the same delay, with the cooldown on its row."""
    store = _RecordingStore()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a":
            return httpx.Response(200, json=_completion(""))
        return httpx.Response(200, text="<html>502 from your proxy</html>")

    async def run():
        router = await _router(store, "a", "b")
        _mount(router, handler)
        with pytest.raises(NoLLMAvailableError):
            await router.chat(make_ring(), [{"role": "user", "content": "hi"}], wait=0)
        return router._pool  # noqa: SLF001

    pool = asyncio.run(run())
    empty, garbage = store.calls
    assert pool.state("a").phase is LifecyclePhase.COOLING
    assert (empty.status, empty.http_status) == (garbage.status, garbage.http_status)
    assert empty.cooldown_until is not None
    assert round((empty.cooldown_until - empty.ts).total_seconds()) == round(
        (garbage.cooldown_until - garbage.ts).total_seconds(),
    )


# --------------------------------------------------------------------------- #
# streams through the pool
# --------------------------------------------------------------------------- #


def test_a_stream_that_never_yields_a_delta_fails_over():
    """Nothing reached the caller, so failover is still open — and the diagnosis says
    the answer was empty, not that the body was never a stream."""
    store = _RecordingStore()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a":
            return _stream_response(_EMPTY_SSE)
        return _stream_response(_sse("ok"))

    async def run():
        router = await _router(store, "a", "b")
        _mount(router, handler)
        return await _drain(router)

    assert asyncio.run(run()) == ["ok"]
    assert [(c.llm_name, c.status) for c in store.calls] == [
        ("a", CallStatus.ERROR),
        ("b", CallStatus.OK),
    ]
    assert "no chat-completion chunks decoded" not in (store.calls[0].error_detail or "")


def test_a_stream_that_never_yields_a_delta_raises_when_it_was_the_last_candidate():
    store = _RecordingStore()

    async def run():
        router = await _router(store, "a")
        _mount(router, lambda _r: _stream_response(_EMPTY_SSE))
        with pytest.raises(NoLLMAvailableError):
            await _drain(router, wait=0)

    asyncio.run(run())
    assert [(c.llm_name, c.status) for c in store.calls] == [("a", CallStatus.ERROR)]


# --------------------------------------------------------------------------- #
# the direct client — no pool, nothing to fail over to
# --------------------------------------------------------------------------- #


def _direct_handler(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=_completion(""))


def test_a_direct_client_raises_on_an_empty_answer():
    """Both clients, one rule: an answer that carried nothing is reported, never
    handed back as a result the caller has to inspect."""

    async def run():
        client = AsyncDirectClient(
            base_url="https://api.example.com/v1",
            model="m",
            api_key="k",
            client=httpx.AsyncClient(transport=httpx.MockTransport(_direct_handler), timeout=1.0),
        )
        with pytest.raises(InvalidProviderResponseError) as exc_info:
            await client.ask("hi")
        await client.aclose()
        return exc_info.value

    err = asyncio.run(run())
    assert err.model == "m"

    sync_client = DirectClient(
        base_url="https://api.example.com/v1",
        model="m",
        api_key="k",
        client=httpx.Client(transport=httpx.MockTransport(_direct_handler), timeout=1.0),
    )
    with pytest.raises(InvalidProviderResponseError):
        sync_client.ask("hi")
    sync_client.close()

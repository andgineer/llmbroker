"""A pooled answer its provider stopped abnormally is a failed attempt: the next model
answers, and the failed row names the stop reason."""

import asyncio
import json
from contextlib import aclosing

import httpx
import pytest
from support import make_ring

from llmbroker.broker.broker import AsyncBroker
from llmbroker.broker.pool import LLMPool
from llmbroker.broker.result import CallReceipt
from llmbroker.broker.router import Router
from llmbroker.direct import AsyncDirectClient
from llmbroker.exceptions import (
    AnswerTooLongError,
    StreamInterruptedError,
    StreamReplacementError,
)
from llmbroker.models import CallStatus, LLMConfig
from llmbroker.standalone.registry import Registry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import FileStore

# What the providers in the curated pool document besides a normal stop, Gemini's
# native names included in case its compatibility layer passes one through.
ABNORMAL = ["length", "content_filter", "error", "SAFETY", "recitation", "OTHER", "sensitive"]
# A length cut is the one the request itself can cause, so it alone cools nothing.
COOLING = [reason for reason in ABNORMAL if reason != "length"]


class _RecordingStore:
    def __init__(self) -> None:
        self.calls: list = []

    async def record(self, call):
        self.calls.append(call)

    async def record_quality(self, call_id, score, *, scope=None):
        pass


async def _router(store, *names: str) -> Router:
    pool = LLMPool()
    for name in names:
        await pool.add(
            LLMConfig(name=name, base_url=f"https://{name}/v1", model="m", api_key_ref="K"),
        )
    return Router(pool, store)


def _mount(router: Router, handler) -> None:
    router._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


def _completion(content: str, finish: str | None) -> dict:
    choice: dict = {"message": {"role": "assistant", "content": content}}
    if finish is not None:
        choice["finish_reason"] = finish
    return {"choices": [choice], "usage": {"completion_tokens": 3}}


def _sse(*deltas: str, finish: str | None = "stop") -> bytes:
    """Gemini's shape: the stop reason rides the last chunk together with the usage."""
    body = b"".join(
        b"data: " + json.dumps({"choices": [{"delta": {"content": d}}]}).encode() + b"\n\n"
        for d in deltas
    )
    last = {"choices": [{"delta": {}, "finish_reason": finish}], "usage": {"total_tokens": 9}}
    return body + b"data: " + json.dumps(last).encode() + b"\n\ndata: [DONE]\n\n"


def _openai_sse(*deltas: str, finish: str, tail: dict) -> bytes:
    """OpenAI's, Groq's and OpenRouter's shape: the stop reason rides its own chunk, and
    a later one without a reason (usage only, or a null reason) may follow it."""
    chunks = [{"choices": [{"delta": {"content": d}, "finish_reason": None}]} for d in deltas]
    chunks += [{"choices": [{"delta": {}, "finish_reason": finish}]}, tail]
    body = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks)
    return body + b"data: [DONE]\n\n"


USAGE_ONLY = {"choices": [], "usage": {"total_tokens": 9}}
NULL_REASON = {"choices": [{"delta": {}, "finish_reason": None}]}


def _stream_response(content) -> httpx.Response:
    return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"})


async def _drain(router: Router, **kwargs) -> list[str]:
    routed = router.stream(
        make_ring(), [{"role": "user", "content": "hi"}], CallReceipt(), **kwargs
    )
    async with aclosing(routed) as deltas:
        return [d async for d in deltas]


# --------------------------------------------------------------------------- #
# completions
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("finish", ["stop", "STOP", "tool_calls", None])
def test_a_normal_stop_is_the_answer(finish):
    store = _RecordingStore()

    async def run():
        router = await _router(store, "a", "b")
        _mount(router, lambda _r: httpx.Response(200, json=_completion("whole", finish)))
        return await router.chat(make_ring(), [{"role": "user", "content": "hi"}])

    result = asyncio.run(run())
    assert (result.text, result.llm_name) == ("whole", "a")
    assert [(c.llm_name, c.status) for c in store.calls] == [("a", CallStatus.OK)]


@pytest.mark.parametrize("finish", COOLING)
def test_an_abnormal_stop_fails_over_and_the_row_names_it(finish):
    store = _RecordingStore()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a":
            return httpx.Response(200, json=_completion("Привет! От", finish))
        return httpx.Response(200, json=_completion("whole", "stop"))

    async def run():
        router = await _router(store, "a", "b")
        _mount(router, handler)
        return await router.chat(make_ring(), [{"role": "user", "content": "hi"}])

    result = asyncio.run(run())
    assert (result.text, result.llm_name) == ("whole", "b")
    assert [(c.llm_name, c.status) for c in store.calls] == [
        ("a", CallStatus.ERROR),
        ("b", CallStatus.OK),
    ]
    assert store.calls[0].error_detail == f"finish_reason={finish}"
    assert store.calls[0].cooldown_until is not None


def test_a_length_cut_fails_over_without_cooling_the_model():
    store = _RecordingStore()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a":
            return httpx.Response(200, json=_completion("Привет! От", "length"))
        return httpx.Response(200, json=_completion("whole", "stop"))

    async def run():
        router = await _router(store, "a", "b")
        _mount(router, handler)
        return await router.chat(make_ring(), [{"role": "user", "content": "hi"}])

    result = asyncio.run(run())
    assert (result.text, result.llm_name) == ("whole", "b")
    assert [(c.llm_name, c.status, c.error_detail) for c in store.calls] == [
        ("a", CallStatus.ERROR, "finish_reason=length"),
        ("b", CallStatus.OK, None),
    ]
    assert store.calls[0].cooldown_until is None


def _oversized(request: httpx.Request) -> httpx.Response:
    """Every model cuts an answer the request asks to be too long, and answers a short one."""
    finish = "length" if b"long" in request.content else "stop"
    return httpx.Response(200, json=_completion("x", finish))


@pytest.mark.parametrize("wait", [2.0, None])
def test_an_answer_every_model_cuts_at_length_fails_the_call_and_cools_nothing(wait):
    """The request, not the models, is at fault: the call ends instead of cooling the
    whole pool and waiting on it, and an unrelated call right after is served."""
    store = _RecordingStore()

    async def run():
        router = await _router(store, "a", "b")
        _mount(router, _oversized)
        with pytest.raises(AnswerTooLongError) as cut:
            await asyncio.wait_for(
                router.chat(make_ring(), [{"role": "user", "content": "long"}], wait=wait),
                timeout=5.0,
            )
        short = await router.chat(make_ring(), [{"role": "user", "content": "hi"}], wait=wait)
        return cut.value, short

    cut, short = asyncio.run(run())
    assert cut.detail == "finish_reason=length"
    assert (short.text, short.llm_name) == ("x", "a")
    assert [(c.llm_name, c.status, c.error_detail) for c in store.calls] == [
        ("a", CallStatus.ERROR, "finish_reason=length"),
        ("b", CallStatus.ERROR, "finish_reason=length"),
        ("a", CallStatus.OK, None),
    ]
    assert all(c.cooldown_until is None for c in store.calls)


# --------------------------------------------------------------------------- #
# ordinary streams
# --------------------------------------------------------------------------- #


def test_a_stream_ending_normally_is_the_answer():
    store = _RecordingStore()

    async def run():
        router = await _router(store, "a", "b")
        _mount(router, lambda _r: _stream_response(_sse("who", "le")))
        return await _drain(router)

    assert asyncio.run(run()) == ["who", "le"]
    assert [(c.llm_name, c.status) for c in store.calls] == [("a", CallStatus.OK)]


@pytest.mark.parametrize("finish", ABNORMAL)
def test_an_abnormal_stop_before_any_delta_fails_over(finish):
    store = _RecordingStore()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a":
            return _stream_response(_sse(finish=finish))
        return _stream_response(_sse("whole"))

    async def run():
        router = await _router(store, "a", "b")
        _mount(router, handler)
        return await _drain(router)

    assert asyncio.run(run()) == ["whole"]
    assert [(c.llm_name, c.status) for c in store.calls] == [
        ("a", CallStatus.ERROR),
        ("b", CallStatus.OK),
    ]
    assert store.calls[0].error_detail == f"finish_reason={finish}"


def test_an_abnormal_stop_after_deltas_ends_an_ordinary_stream_without_splicing():
    """Text already reached the caller of an unraced stream, so no other model may
    continue it: the stub is reported as a dead stream, never as a finished answer."""
    store = _RecordingStore()

    async def run():
        router = await _router(store, "a", "b")
        _mount(router, lambda _r: _stream_response(_sse("Привет! От", finish="content_filter")))
        seen: list[str] = []
        routed = router.stream(make_ring(), [{"role": "user", "content": "hi"}], CallReceipt())
        async with aclosing(routed) as deltas:
            with pytest.raises(StreamInterruptedError):
                async for delta in deltas:
                    seen.append(delta)
        return seen

    assert asyncio.run(run()) == ["Привет! От"]
    assert [(c.llm_name, c.status, c.error_detail) for c in store.calls] == [
        ("a", CallStatus.ERROR, "finish_reason=content_filter"),
    ]


def test_a_stream_every_model_cuts_at_length_before_any_delta_fails_and_cools_nothing():
    store = _RecordingStore()

    async def run():
        router = await _router(store, "a", "b")
        _mount(router, lambda _r: _stream_response(_sse(finish="length")))
        with pytest.raises(AnswerTooLongError):
            await asyncio.wait_for(_drain(router), timeout=5.0)

    asyncio.run(run())
    assert [(c.llm_name, c.error_detail) for c in store.calls] == [
        ("a", "finish_reason=length"),
        ("b", "finish_reason=length"),
    ]
    assert all(c.cooldown_until is None for c in store.calls)


def test_a_length_cut_after_deltas_ends_the_stream_without_cooling_the_model():
    store = _RecordingStore()

    async def run():
        router = await _router(store, "a", "b")
        _mount(router, lambda _r: _stream_response(_sse("Привет! От", finish="length")))
        with pytest.raises(StreamInterruptedError):
            await _drain(router)

    asyncio.run(run())
    assert [(c.llm_name, c.error_detail) for c in store.calls] == [("a", "finish_reason=length")]
    assert store.calls[0].cooldown_until is None


@pytest.mark.parametrize("tail", [USAGE_ONLY, NULL_REASON], ids=["usage-only", "null-reason"])
def test_a_normal_stop_followed_by_a_chunk_without_a_reason_is_the_answer(tail):
    store = _RecordingStore()

    async def run():
        router = await _router(store, "a", "b")
        _mount(
            router, lambda _r: _stream_response(_openai_sse("who", "le", finish="stop", tail=tail))
        )
        return await _drain(router)

    assert asyncio.run(run()) == ["who", "le"]
    assert [(c.llm_name, c.status) for c in store.calls] == [("a", CallStatus.OK)]


# --------------------------------------------------------------------------- #
# a raced stream: the shape echo-words runs
# --------------------------------------------------------------------------- #


async def _broker(tmp_path, handler, *names: str) -> AsyncBroker:
    f = tmp_path / "llms.toml"
    f.write_text(
        "".join(
            f'[[llms]]\nname="{n}"\nbase_url="https://{n}/v1"\nmodel="m"\napi_key_ref="K"\n'
            for n in names
        ),
    )
    broker = AsyncBroker(
        registry=Registry(f),
        secrets=DictSecrets({"K": "test"}),
        store=FileStore(tmp_path / "store"),
        sync=None,
    )
    await broker.ensure_pool()
    broker._router._http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        timeout=5.0,
    )
    return broker


@pytest.mark.parametrize("finish", ["length", "content_filter", "OTHER"])
def test_a_raced_stub_is_replaced_by_the_other_lanes_whole_answer(tmp_path, finish):
    """The observed failure: the preferred lane streams a few tokens and stops. The
    reader is handed the reserve's complete answer, not the stub."""
    stub_done = asyncio.Event()

    async def reserve():
        await stub_done.wait()
        yield _sse("the whole answer")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a":
            stub_done.set()
            return _stream_response(_sse("Привет! От", finish=finish))
        return _stream_response(reserve())

    async def run():
        broker = await _broker(tmp_path, handler, "a", "b")
        async with broker:
            handle = broker.stream("hi", fastest_of=2)
            seen: list[str] = []
            with pytest.raises(StreamReplacementError) as replaced:
                async for delta in handle:
                    seen.append(delta)
            rows = await broker.calls(limit=10)
            return seen, replaced.value, rows

    seen, replaced, rows = asyncio.run(run())
    assert seen == ["Привет! От"]
    assert (replaced.streamed_llm_name, replaced.replacement.llm_name) == ("a", "b")
    assert replaced.replacement.text == "the whole answer"
    by_model = {row.llm_name: row for row in rows}
    assert by_model["a"].status is CallStatus.ERROR
    assert by_model["a"].error_detail == f"finish_reason={finish}"
    assert by_model["b"].status is CallStatus.OK


def test_a_raced_stub_whose_reason_a_usage_only_chunk_follows_is_still_replaced(tmp_path):
    """A chunk without a reason after the abnormal one must not erase it."""
    stub_done = asyncio.Event()

    async def reserve():
        await stub_done.wait()
        yield _openai_sse("the whole answer", finish="stop", tail=USAGE_ONLY)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a":
            stub_done.set()
            return _stream_response(
                _openai_sse("Привет! От", finish="content_filter", tail=USAGE_ONLY),
            )
        return _stream_response(reserve())

    async def run():
        broker = await _broker(tmp_path, handler, "a", "b")
        async with broker:
            handle = broker.stream("hi", fastest_of=2)
            with pytest.raises(StreamReplacementError) as replaced:
                async for _ in handle:
                    pass
            return replaced.value

    replaced = asyncio.run(run())
    assert (replaced.streamed_llm_name, replaced.replacement.llm_name) == ("a", "b")
    assert replaced.replacement.text == "the whole answer"


# --------------------------------------------------------------------------- #
# a model reached by name is not judged
# --------------------------------------------------------------------------- #


def test_a_direct_client_returns_a_cut_answer():
    """A direct caller may set its own token cap and has no other model to ask."""

    async def run():
        client = AsyncDirectClient(
            base_url="https://api.example.com/v1",
            model="m",
            api_key="k",
            client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _r: httpx.Response(200, json=_completion("cut", "length")),
                ),
                timeout=1.0,
            ),
        )
        result = await client.ask("hi")
        await client.aclose()
        return result.text

    assert asyncio.run(run()) == "cut"

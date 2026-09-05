"""Pool streaming: failover before the first delta, no failover after it."""

import asyncio
import json
from contextlib import aclosing

import httpx
import pytest

from llmbroker.broker import router as router_module
from llmbroker.broker.broker import AsyncBroker
from llmbroker.broker.pool import LLMPool
from llmbroker.broker.result import CallReceipt
from llmbroker.broker.router import Router
from llmbroker.direct import AsyncDirectClient
from llmbroker.exceptions import (
    InvalidProviderResponseError,
    NoLLMAvailableError,
    ProviderError,
    StreamInterruptedError,
)
from llmbroker.models import CallStatus, LifecyclePhase, LLMConfig
from llmbroker.standalone.registry import Registry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import FileStore

from support import make_ring


class _RecordingStore:
    def __init__(self) -> None:
        self.calls: list = []

    async def record(self, call):
        self.calls.append(call)

    async def record_quality(self, call_id, score, *, scope=None):
        pass


def _cfg(name: str, host: str) -> LLMConfig:
    return LLMConfig(name=name, base_url=f"https://{host}/v1", model="m", api_key_ref="K")


async def _pool(*cfgs: LLMConfig) -> LLMPool:
    pool = LLMPool()
    for cfg in cfgs:
        await pool.add(cfg)
    return pool


def _sse(*deltas: str) -> bytes:
    body = b"".join(
        b'data: {"choices": [{"delta": {"content": "%s"}}]}\n\n' % d.encode() for d in deltas
    )
    return body + b'data: {"choices": [], "usage": {"total_tokens": 7}}\n\n' + b"data: [DONE]\n\n"


def _mount(router: Router, handler) -> None:
    router._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)  # noqa: SLF001


def _stream(router: Router, **kwargs):
    """One routed stream over a throwaway receipt, for tests reading only the deltas."""
    return router.stream(make_ring(), [{"role": "user", "content": "hi"}], CallReceipt(), **kwargs)


async def _drain(router: Router, **kwargs) -> list[str]:
    return [d async for d in _stream(router, **kwargs)]


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #


def test_stream_yields_deltas_and_journals_one_ok_row():
    store = _RecordingStore()

    async def run():
        pool = await _pool(_cfg("a", "a"))
        router = Router(pool, store)
        _mount(
            router,
            lambda _r: httpx.Response(
                200,
                content=_sse("Hel", "lo"),
                headers={"content-type": "text/event-stream"},
            ),
        )
        return await _drain(router, operation="write")

    assert asyncio.run(run()) == ["Hel", "lo"]
    (row,) = store.calls
    assert (row.llm_name, row.status, row.operation) == ("a", CallStatus.OK, "write")
    assert row.usage.total_tokens == 7  # final usage chunk is journaled
    assert row.latency_ms is not None


def test_stream_arrives_incrementally():
    """Each delta reaches the consumer before the next one is produced."""
    released = asyncio.Event()

    async def body():
        yield b'data: {"choices": [{"delta": {"content": "one"}}]}\n\n'
        await released.wait()
        yield b'data: {"choices": [{"delta": {"content": "two"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    async def run():
        pool = await _pool(_cfg("a", "a"))
        router = Router(pool, _RecordingStore())
        _mount(
            router,
            lambda _r: httpx.Response(
                200, content=body(), headers={"content-type": "text/event-stream"}
            ),
        )
        seen: list[str] = []
        async for delta in _stream(router):
            seen.append(delta)
            released.set()  # only reached if "one" arrived without "two" being ready
        return seen

    assert asyncio.run(run()) == ["one", "two"]


# --------------------------------------------------------------------------- #
# failover before the first delta
# --------------------------------------------------------------------------- #


def test_stream_fails_over_on_429_before_first_delta():
    store = _RecordingStore()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a":
            return httpx.Response(429, text="slow down")
        return httpx.Response(
            200, content=_sse("ok"), headers={"content-type": "text/event-stream"}
        )

    async def run():
        pool = await _pool(_cfg("a", "a"), _cfg("b", "b"))
        router = Router(pool, store)
        _mount(router, handler)
        deltas = await _drain(router)
        return deltas, pool

    deltas, pool = asyncio.run(run())
    assert deltas == ["ok"]
    statuses = [(c.llm_name, c.status) for c in store.calls]
    assert statuses == [("a", CallStatus.RATE_LIMITED), ("b", CallStatus.OK)]
    assert store.calls[0].cooldown_until is not None  # the 429 cooled 'a' down


def test_stream_fails_over_on_transport_error_before_first_delta():
    store = _RecordingStore()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a":
            raise httpx.ConnectError("boom")
        return httpx.Response(
            200, content=_sse("ok"), headers={"content-type": "text/event-stream"}
        )

    async def run():
        pool = await _pool(_cfg("a", "a"), _cfg("b", "b"))
        router = Router(pool, store)
        _mount(router, handler)
        return await _drain(router)

    assert asyncio.run(run()) == ["ok"]
    assert [c.llm_name for c in store.calls] == ["a", "b"]


def test_stream_raises_when_every_candidate_fails():
    async def run():
        pool = await _pool(_cfg("a", "a"), _cfg("b", "b"))
        router = Router(pool, _RecordingStore())
        _mount(router, lambda _r: httpx.Response(503, text="down"))
        with pytest.raises(NoLLMAvailableError):
            await _drain(router, wait=0.5)

    asyncio.run(run())


def test_a_stream_that_says_nothing_for_the_whole_budget_is_cooled():
    """No delta for the whole budget is silence, not slowness: the caller still gets its
    own expired wait, and the model is cooled like any other failed attempt."""
    store = _RecordingStore()

    async def body():
        await asyncio.sleep(5)
        yield b'data: {"choices": [{"delta": {"content": "late"}}]}\n\n'

    async def run():
        pool = await _pool(_cfg("a", "a"))
        router = Router(pool, store)
        _mount(
            router,
            lambda _r: httpx.Response(
                200, content=body(), headers={"content-type": "text/event-stream"}
            ),
        )
        with pytest.raises(NoLLMAvailableError) as excinfo:
            await _drain(router, wait=0.2)
        return excinfo.value, pool

    exc, pool = asyncio.run(run())
    assert exc.reason == "timeout"
    assert pool.state("a").phase is LifecyclePhase.COOLING
    assert pool.state("a").fail_count == 1
    (row,) = store.calls
    assert row.status is CallStatus.ERROR
    assert row.cooldown_until is not None
    assert row.budget_ms is not None  # the miss still teaches ordering


def test_wait_does_not_bound_a_slow_consumer():
    """Once deltas flow, the budget is spent: a consumer that dawdles between them
    must not kill its own stream."""

    async def run():
        pool = await _pool(_cfg("a", "a"))
        router = Router(pool, _RecordingStore())
        _mount(
            router,
            lambda _r: httpx.Response(
                200,
                content=_sse("one", "two"),
                headers={"content-type": "text/event-stream"},
            ),
        )
        seen: list[str] = []
        async for delta in _stream(router, wait=0.3):
            seen.append(delta)
            await asyncio.sleep(0.25)  # well past the budget, between deltas
        return seen

    assert asyncio.run(run()) == ["one", "two"]


# --------------------------------------------------------------------------- #
# after the first delta
# --------------------------------------------------------------------------- #


def test_stream_error_after_first_delta_propagates_and_journals_error():
    store = _RecordingStore()

    async def body():
        yield b'data: {"choices": [{"delta": {"content": "par"}}]}\n\n'
        raise httpx.ReadError("connection dropped")

    async def run():
        pool = await _pool(_cfg("a", "a"), _cfg("b", "b"))
        router = Router(pool, store)
        _mount(
            router,
            lambda _r: httpx.Response(
                200, content=body(), headers={"content-type": "text/event-stream"}
            ),
        )
        seen: list[str] = []
        with pytest.raises(StreamInterruptedError) as excinfo:
            async for delta in _stream(router):
                seen.append(delta)
        return seen, excinfo.value

    seen, exc = asyncio.run(run())
    assert seen == ["par"]  # the deltas already delivered stand
    assert exc.llm_name == "a"
    (row,) = store.calls  # no failover onto 'b': one attempt, one ERROR row
    assert (row.llm_name, row.status) == ("a", CallStatus.ERROR)


def test_non_sse_200_cools_down_and_fails_over():
    """A 200 that is not an SSE stream is a garbage 200: same verdict as on `chat`."""
    store = _RecordingStore()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a":
            return httpx.Response(
                200,
                content=b"<html><body>502 from your proxy</body></html>",
                headers={"content-type": "text/html"},
            )
        return httpx.Response(
            200, content=_sse("ok"), headers={"content-type": "text/event-stream"}
        )

    async def run():
        pool = await _pool(_cfg("a", "a"), _cfg("b", "b"))
        router = Router(pool, store)
        _mount(router, handler)
        return await _drain(router), pool

    deltas, pool = asyncio.run(run())
    assert deltas == ["ok"]
    assert [(c.llm_name, c.status) for c in store.calls] == [
        ("a", CallStatus.ERROR),
        ("b", CallStatus.OK),
    ]
    assert pool.state("a").phase is LifecyclePhase.COOLING
    assert "no chat-completion chunks decoded" in (store.calls[0].error_detail or "")


def test_sse_framed_error_payload_cools_down_and_fails_over():
    """A 200 whose SSE body carries only a provider error is a garbage 200 too:
    it decodes fine, but no chunk is a chat completion."""
    store = _RecordingStore()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a":
            return httpx.Response(
                200,
                content=b'data: {"error": {"message": "upstream rate limit", "code": 429}}\n\n'
                b"data: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(
            200, content=_sse("ok"), headers={"content-type": "text/event-stream"}
        )

    async def run():
        pool = await _pool(_cfg("a", "a"), _cfg("b", "b"))
        router = Router(pool, store)
        _mount(router, handler)
        return await _drain(router), pool

    deltas, pool = asyncio.run(run())
    assert deltas == ["ok"]  # failed over instead of handing back an empty answer
    assert [(c.llm_name, c.status) for c in store.calls] == [
        ("a", CallStatus.ERROR),
        ("b", CallStatus.OK),
    ]
    assert pool.state("a").phase is LifecyclePhase.COOLING


def test_garbage_200_reads_the_same_from_the_router_and_the_direct_client(monkeypatch):
    """One reader decides what makes a body a chat-completion stream, so the pooled
    path raises byte-identically to the direct client — rendered message as well as
    detail, which alone carries no model name and would not catch a divergence."""
    garbage = b'data: {"error": {"message": "upstream rate limit"}}\n\ndata: [DONE]\n\n'
    store = _RecordingStore()
    routed: list[Exception] = []
    real_classify = router_module._classify  # noqa: SLF001

    def spy(exc, *, budget_bound):
        routed.append(exc)
        return real_classify(exc, budget_bound=budget_bound)

    monkeypatch.setattr(router_module, "_classify", spy)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a":
            return httpx.Response(
                200, content=garbage, headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(
            200, content=_sse("ok"), headers={"content-type": "text/event-stream"}
        )

    async def run():
        pool = await _pool(_cfg("a", "a"), _cfg("b", "b"))
        router = Router(pool, store)
        _mount(router, handler)
        await _drain(router)

        client = AsyncDirectClient(
            base_url="https://a/v1",
            model="a",
            api_key="k",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0),
        )
        with pytest.raises(InvalidProviderResponseError) as exc_info:
            _ = [d async for d in client.stream("hi")]
        await client.aclose()
        return exc_info.value

    err = asyncio.run(run())
    assert "no chat-completion chunks decoded" in (err.detail or "")
    assert store.calls[0].error_detail == err.detail
    (routed_err,) = routed
    assert str(routed_err) == str(err)
    assert routed_err.detail == err.detail


@pytest.mark.parametrize(
    "payload",
    [b"5", b'"hi"', b"[1, 2, 3]"],
    ids=["number", "string", "array"],
)
def test_non_object_sse_payload_is_a_garbage_200(payload):
    """A payload decoding to a JSON scalar or array is not a chunk: it must reach
    neither the router nor the direct client as a raw AttributeError/TypeError."""
    store = _RecordingStore()
    garbage = b"data: " + payload + b"\n\ndata: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a":
            return httpx.Response(
                200, content=garbage, headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(
            200, content=_sse("ok"), headers={"content-type": "text/event-stream"}
        )

    async def run():
        pool = await _pool(_cfg("a", "a"), _cfg("b", "b"))
        router = Router(pool, store)
        _mount(router, handler)
        deltas = await _drain(router)

        client = AsyncDirectClient(
            base_url="https://a/v1",
            model="a",
            api_key="k",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0),
        )
        with pytest.raises(InvalidProviderResponseError) as exc_info:
            _ = [d async for d in client.stream("hi")]
        await client.aclose()
        return deltas, pool, exc_info.value

    deltas, pool, err = asyncio.run(run())
    assert deltas == ["ok"]  # failed over instead of escaping raw
    assert [(c.llm_name, c.status) for c in store.calls] == [
        ("a", CallStatus.ERROR),
        ("b", CallStatus.OK),
    ]
    assert pool.state("a").phase is LifecyclePhase.COOLING
    assert "no chat-completion chunks decoded" in (store.calls[0].error_detail or "")
    assert "no chat-completion chunks decoded" in (err.detail or "")


def test_a_non_object_payload_among_real_chunks_leaves_the_stream_working():
    """The guard skips one payload, not the answer around it."""
    store = _RecordingStore()

    async def run():
        pool = await _pool(_cfg("a", "a"))
        router = Router(pool, store)
        _mount(
            router,
            lambda _r: httpx.Response(
                200,
                content=b"data: [1, 2, 3]\n\n"
                b'data: {"choices": [{"delta": {"content": "ok"}}]}\n\n'
                b"data: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            ),
        )
        return await _drain(router), pool

    deltas, pool = asyncio.run(run())
    assert deltas == ["ok"]
    assert [(c.llm_name, c.status) for c in store.calls] == [("a", CallStatus.OK)]
    assert pool.state("a").phase is LifecyclePhase.AVAILABLE


_MALFORMED_CHUNKS = [
    b'{"choices": [{"delta": null}]}',
    b'{"choices": [null]}',
    b'{"choices": ["x"]}',
    b'{"choices": {"delta": {"content": "hi"}}}',
]


@pytest.mark.parametrize(
    "chunk",
    _MALFORMED_CHUNKS,
    ids=["delta-null", "choice-null", "choice-not-an-object", "choices-not-a-list"],
)
def test_malformed_chunk_shape_is_a_garbage_200(chunk):
    """A chunk that is an object carrying `choices` still counts as a completion, so
    a shape the extractor cannot read must fail over rather than escape raw."""
    store = _RecordingStore()
    garbage = b"data: " + chunk + b"\n\ndata: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a":
            return httpx.Response(
                200, content=garbage, headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(
            200, content=_sse("ok"), headers={"content-type": "text/event-stream"}
        )

    async def run():
        pool = await _pool(_cfg("a", "a"), _cfg("b", "b"))
        router = Router(pool, store)
        _mount(router, handler)
        deltas = await _drain(router)

        client = AsyncDirectClient(
            base_url="https://a/v1",
            model="a",
            api_key="k",
            client=httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0),
        )
        with pytest.raises(InvalidProviderResponseError) as exc_info:
            _ = [d async for d in client.stream("hi")]
        await client.aclose()
        return deltas, pool, exc_info.value

    deltas, pool, err = asyncio.run(run())
    assert deltas == ["ok"]  # failed over instead of escaping raw
    assert [(c.llm_name, c.status) for c in store.calls] == [
        ("a", CallStatus.ERROR),
        ("b", CallStatus.OK),
    ]
    assert pool.state("a").phase is LifecyclePhase.COOLING
    assert err.model == "a"


def test_a_malformed_chunk_after_the_first_delta_interrupts_the_stream():
    """Past the first delta the answer is already partly the caller's: the model is
    cooled and the caller is told, rather than the truncation passing for an ending."""
    store = _RecordingStore()

    async def run():
        pool = await _pool(_cfg("a", "a"))
        router = Router(pool, store)
        _mount(
            router,
            lambda _r: httpx.Response(
                200,
                content=b'data: {"choices": [{"delta": {"content": "par"}}]}\n\n'
                b'data: {"choices": [{"delta": null}]}\n\n'
                b"data: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            ),
        )
        seen: list[str] = []
        with pytest.raises(StreamInterruptedError) as exc_info:
            async for delta in _stream(router):
                seen.append(delta)
        return seen, pool, exc_info.value

    seen, pool, err = asyncio.run(run())
    assert seen == ["par"]  # the delta already delivered stands
    assert err.llm_name == "a"
    (row,) = store.calls
    assert (row.llm_name, row.status) == ("a", CallStatus.ERROR)
    assert row.cooldown_until is not None
    assert pool.state("a").phase is LifecyclePhase.COOLING


def test_a_finish_chunk_with_no_choices_is_not_malformed():
    """The guard must not fire on the ordinary preamble and usage shapes."""
    store = _RecordingStore()

    async def run():
        pool = await _pool(_cfg("a", "a"))
        router = Router(pool, store)
        _mount(
            router,
            lambda _r: httpx.Response(
                200,
                content=b'data: {"choices": []}\n\n'
                b'data: {"choices": [{"delta": {"content": "ok"}}]}\n\n'
                b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}],'
                b' "usage": {"total_tokens": 7}}\n\n'
                b"data: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            ),
        )
        return await _drain(router), pool

    deltas, pool = asyncio.run(run())
    assert deltas == ["ok"]
    (row,) = store.calls
    assert (row.llm_name, row.status) == ("a", CallStatus.OK)
    assert row.usage.total_tokens == 7  # noqa: PLR2004
    assert pool.state("a").phase is LifecyclePhase.AVAILABLE


def test_stream_reraises_the_provider_error_when_every_candidate_rejects_the_request():
    """A 4xx is the request's fault on the streaming path too: nothing is cooled, and
    the provider's own error beats a generic `NoLLMAvailableError`."""
    store = _RecordingStore()

    async def run():
        pool = await _pool(_cfg("a", "a"), _cfg("b", "b"))
        router = Router(pool, store)
        _mount(router, lambda _r: httpx.Response(400, text="messages[0].role is invalid"))
        with pytest.raises(ProviderError) as exc_info:
            await _drain(router)
        return exc_info.value, pool

    err, pool = asyncio.run(run())
    assert err.status == 400  # noqa: PLR2004
    assert err.detail == "messages[0].role is invalid"
    assert [(c.llm_name, c.status) for c in store.calls] == [
        ("a", CallStatus.ERROR),
        ("b", CallStatus.ERROR),
    ]
    assert all(c.cooldown_until is None for c in store.calls)
    assert pool.state("a").phase is LifecyclePhase.AVAILABLE
    assert pool.state("b").phase is LifecyclePhase.AVAILABLE


def test_abandoned_stream_releases_the_slot():
    """A consumer that breaks out must not cost the model a unit of `parallel`."""
    store = _RecordingStore()

    async def run():
        cfg = LLMConfig(name="a", base_url="https://a/v1", model="m", api_key_ref="K", parallel=1)
        pool = await _pool(cfg)
        router = Router(pool, store)
        _mount(
            router,
            lambda _r: httpx.Response(
                200,
                content=_sse("one", "two", "three"),
                headers={"content-type": "text/event-stream"},
            ),
        )
        async for delta in _stream(router):
            assert delta == "one"
            break
        # a second stream can only start if the first released its only slot
        return await _drain(router)

    assert asyncio.run(run()) == ["one", "two", "three"]
    assert [c.status for c in store.calls] == [CallStatus.OK, CallStatus.OK]


def test_held_iterator_releases_the_slot_when_closed():
    """A consumer that keeps the iterator past the loop owns closing it — that is
    the only signal an async generator has, and `aclosing` is how it is sent."""
    store = _RecordingStore()

    async def run():
        cfg = LLMConfig(name="a", base_url="https://a/v1", model="m", api_key_ref="K", parallel=1)
        pool = await _pool(cfg)
        router = Router(pool, store)
        _mount(
            router,
            lambda _r: httpx.Response(
                200,
                content=_sse("one", "two", "three"),
                headers={"content-type": "text/event-stream"},
            ),
        )
        async with aclosing(_stream(router)) as deltas:
            async for delta in deltas:
                assert delta == "one"
                break
        assert [c.status for c in store.calls] == [CallStatus.OK]  # settled on close
        return await _drain(router)  # the only slot is free again

    assert asyncio.run(run()) == ["one", "two", "three"]


# --------------------------------------------------------------------------- #
# broker façade
# --------------------------------------------------------------------------- #


def test_broker_stream_end_to_end(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text('[[llms]]\nname="a"\nbase_url="https://a/v1"\nmodel="m"\napi_key_ref="K"\n')

    async def run():
        broker = AsyncBroker(
            registry=Registry(f),
            secrets=DictSecrets({"K": "test"}),
            store=FileStore(tmp_path / "store"),
            sync=None,
        )
        async with broker:
            await broker.ensure_pool()
            broker._router._http_client = httpx.AsyncClient(  # noqa: SLF001
                transport=httpx.MockTransport(
                    lambda _r: httpx.Response(
                        200,
                        content=_sse("po", "ol"),
                        headers={"content-type": "text/event-stream"},
                    ),
                ),
                timeout=5.0,
            )
            return [d async for d in broker.stream("hi", operation="write")]

    assert asyncio.run(run()) == ["po", "ol"]


# --------------------------------------------------------------------------- #
# the handle: what answered, and what it spent
# --------------------------------------------------------------------------- #


async def _streaming_broker(tmp_path, handler, *names: str) -> AsyncBroker:
    """A provisioned broker over ``names``, answering through ``handler``."""
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
    broker._router._http_client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(handler),
        timeout=5.0,
    )
    return broker


def _empty_sse(_request: httpx.Request) -> httpx.Response:
    """A well-formed completion carrying no text at all — the shape the free tier
    really answers with, not a garbage body."""
    return httpx.Response(
        200,
        content=b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n'
        b'data: {"choices": [], "usage": {"total_tokens": 4}}\n\n'
        b"data: [DONE]\n\n",
        headers={"content-type": "text/event-stream"},
    )


def _ok_sse(*deltas: str):
    return lambda _r: httpx.Response(
        200, content=_sse(*deltas), headers={"content-type": "text/event-stream"}
    )


def test_stream_handle_names_the_model_that_answered(tmp_path):
    async def run():
        broker = await _streaming_broker(tmp_path, _ok_sse("Hel", "lo"), "a")
        async with broker:
            handle = broker.stream("hi", operation="write")
            deltas = [d async for d in handle]
            (row,) = await broker.calls(limit=10)
            return deltas, handle.llm_name, handle.call_id, handle.operation, row

    deltas, llm_name, call_id, operation, row = asyncio.run(run())
    assert deltas == ["Hel", "lo"]
    assert row.status is CallStatus.OK
    assert (llm_name, call_id, operation) == (row.llm_name, row.id, "write")


def test_stream_handle_identity_is_unset_before_the_first_delta(tmp_path):
    """Failover may still move a call until then, so an earlier value would name a
    model that did not answer."""
    opened = asyncio.Event()
    released = asyncio.Event()

    async def body():
        await released.wait()
        yield b'data: {"choices": [{"delta": {"content": "late"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    def handler(_request: httpx.Request) -> httpx.Response:
        opened.set()
        return httpx.Response(200, content=body(), headers={"content-type": "text/event-stream"})

    async def run():
        broker = await _streaming_broker(tmp_path, handler, "a")
        async with broker:
            handle = broker.stream("hi")
            deltas = handle.__aiter__()
            pulling = asyncio.ensure_future(anext(deltas))
            await opened.wait()  # the request is in flight, still no delta
            before = (handle.llm_name, handle.call_id, handle.usage)
            released.set()
            first = await pulling
            after = (handle.llm_name, handle.call_id)
            await handle.aclose()
            return before, first, after

    before, first, after = asyncio.run(run())
    assert before == (None, None, None)
    assert first == "late"
    assert after[0] == "a" and after[1] is not None


def test_stream_handle_identity_survives_failover(tmp_path):
    """The first candidate 429s before any delta; the handle names the one that
    actually answered, whichever the pool reached for first."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url.host))
        if len(seen) == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(
            200, content=_sse("ok"), headers={"content-type": "text/event-stream"}
        )

    async def run():
        broker = await _streaming_broker(tmp_path, handler, "a", "b")
        async with broker:
            handle = broker.stream("hi")
            deltas = [d async for d in handle]
            rows = await broker.calls(limit=10)
            return deltas, handle.llm_name, handle.call_id, rows

    deltas, llm_name, call_id, rows = asyncio.run(run())
    assert deltas == ["ok"]
    cooled = next(r for r in rows if r.status is CallStatus.RATE_LIMITED)
    answered = next(r for r in rows if r.status is CallStatus.OK)
    assert llm_name == answered.llm_name != cooled.llm_name
    assert call_id == answered.id


def test_stream_handle_identity_matches_the_interrupted_stream_error(tmp_path):
    """A mid-stream death names the model on the exception; the handle names the
    same one, so a host has it whichever it reaches for."""

    async def body():
        yield b'data: {"choices": [{"delta": {"content": "par"}}]}\n\n'
        raise httpx.ReadError("connection dropped")

    async def run():
        broker = await _streaming_broker(
            tmp_path,
            lambda _r: httpx.Response(
                200, content=body(), headers={"content-type": "text/event-stream"}
            ),
            "a",
        )
        async with broker:
            handle = broker.stream("hi")
            seen: list[str] = []
            with pytest.raises(StreamInterruptedError) as excinfo:
                async for delta in handle:
                    seen.append(delta)
            return seen, handle.llm_name, excinfo.value.llm_name

    seen, llm_name, error_name = asyncio.run(run())
    assert seen == ["par"]
    assert llm_name == error_name == "a"


def test_stream_handle_identity_stands_after_an_abandoned_stream(tmp_path):
    """A consumer that breaks out still got an answer — the handle keeps naming what
    produced it, so the partial reply is still rateable."""

    async def run():
        broker = await _streaming_broker(tmp_path, _ok_sse("one", "two", "three"), "a")
        async with broker:
            handle = broker.stream("hi")
            async for delta in handle:
                assert delta == "one"
                break
            await handle.aclose()
            (row,) = await broker.calls(limit=10)
            return handle.llm_name, handle.call_id, row

    llm_name, call_id, row = asyncio.run(run())
    assert row.status is CallStatus.OK
    assert (llm_name, call_id) == (row.llm_name, row.id)


def test_stream_handle_reports_usage_after_completion(tmp_path):
    """Token counts reach the caller nowhere else on this path."""

    async def run():
        broker = await _streaming_broker(tmp_path, _ok_sse("Hel", "lo"), "a")
        async with broker:
            handle = broker.stream("hi")
            during: list[object] = []
            async for _ in handle:
                during.append(handle.usage)
            return during, handle.usage

    during, usage = asyncio.run(run())
    assert during[0] is None  # not known until the attempt is over
    assert usage.total_tokens == 7  # noqa: PLR2004


def test_stream_handle_records_quality(tmp_path):
    """Rating off the handle needs no key of the caller's and no journal read."""

    async def run():
        broker = await _streaming_broker(tmp_path, _ok_sse("ok"), "a")
        async with broker:
            handle = broker.stream("hi", operation="write")
            _ = [d async for d in handle]
            await handle.record_quality(0.25)
            return await broker.calls(limit=10)

    (row,) = asyncio.run(run())
    assert row.score == 0.25  # noqa: PLR2004


def test_stream_handle_record_quality_before_an_answer_raises(tmp_path):
    """There is no call to rate yet: nothing has answered, and failover may still move."""

    async def run():
        broker = await _streaming_broker(tmp_path, _ok_sse("ok"), "a")
        async with broker:
            handle = broker.stream("hi")
            with pytest.raises(ValueError, match="not in the journal yet"):
                await handle.record_quality(1.0)
            await handle.aclose()
            return await broker.calls(limit=10)

    assert asyncio.run(run()) == []  # nothing was journaled: no request was opened


def test_stream_handle_record_quality_mid_stream_raises(tmp_path):
    """A rating written before the call's own row would be dropped by a projection
    that folds the two in one pass, so mid-stream it is refused rather than lost."""

    async def run():
        broker = await _streaming_broker(tmp_path, _ok_sse("one", "two"), "a")
        async with broker:
            handle = broker.stream("hi", operation="write")
            named: list[str | None] = []
            async for _delta in handle:
                named.append(handle.llm_name)
                with pytest.raises(ValueError, match="not in the journal yet"):
                    await handle.record_quality(0.5)
            await handle.record_quality(0.5)  # the answer is over: now it lands
            return named, await broker.calls(limit=10)

    named, (row,) = asyncio.run(run())
    assert named == ["a", "a"]  # named all along — it is the rating that had to wait
    assert row.score == 0.5  # noqa: PLR2004


def test_stream_handle_rating_survives_a_broken_off_stream(tmp_path):
    """Stopping early and rating what arrived: the close settles the call, so the
    score is readable back rather than silently dropped."""

    async def run():
        broker = await _streaming_broker(tmp_path, _ok_sse("one", "two", "three"), "a")
        async with broker:
            handle = broker.stream("hi", operation="write")
            async for _delta in handle:
                break
            await handle.aclose()
            await handle.record_quality(0.0)
            return await broker.calls(limit=10)

    (row,) = asyncio.run(run())
    assert (row.status, row.score) == (CallStatus.OK, 0.0)


def test_a_stream_handle_never_names_an_answer_that_carried_no_delta(tmp_path):
    """An answer with no delta is no answer: the last candidate leaves the caller an
    exception, and nothing to rate — no model named, no OK row, no score."""

    async def run():
        broker = await _streaming_broker(tmp_path, _empty_sse, "a")
        async with broker:
            handle = broker.stream("hi", operation="write", wait=0)
            with pytest.raises(NoLLMAvailableError):
                async for _delta in handle:
                    pass
            with pytest.raises(ValueError, match="not in the journal"):
                await handle.record_quality(1.0)
            (row,) = await broker.calls(limit=10)
            return handle.llm_name, row

    llm_name, row = asyncio.run(run())
    assert llm_name is None
    assert row.status is CallStatus.ERROR
    assert row.score is None


# --------------------------------------------------------------------------- #
# response_format on the streaming path
# --------------------------------------------------------------------------- #

_SCHEMA = {
    "type": "json_schema",
    "json_schema": {"name": "card", "schema": {"type": "object"}, "strict": True},
}


def _stream_bodies(responder, **kwargs) -> tuple[list[dict], list[str]]:
    """Drain one routed stream over a mock transport, returning every body posted."""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return responder(len(bodies))

    async def run():
        router = Router(await _pool(_cfg("a", "a"), _cfg("b", "b")), _RecordingStore())
        _mount(router, handler)
        return await _drain(router, **kwargs)

    return bodies, asyncio.run(run())


def _sse_ok(_n: int) -> httpx.Response:
    return httpx.Response(
        200,
        content=_sse("hi"),
        headers={"content-type": "text/event-stream"},
    )


def test_stream_response_format_reaches_the_body_and_keeps_the_streaming_keys():
    bodies, deltas = _stream_bodies(_sse_ok, response_format=_SCHEMA)
    assert deltas == ["hi"]
    assert bodies[0]["response_format"] == _SCHEMA
    assert bodies[0]["stream"] is True
    assert bodies[0]["stream_options"] == {"include_usage": True}


def test_stream_response_format_survives_a_failover_unchanged():
    def responder(n: int) -> httpx.Response:
        return httpx.Response(500, text="nope") if n == 1 else _sse_ok(n)

    bodies, deltas = _stream_bodies(responder, response_format=_SCHEMA)
    assert deltas == ["hi"]
    assert len(bodies) == 2
    assert bodies[0]["response_format"] == bodies[1]["response_format"] == _SCHEMA


def test_omitting_response_format_streams_the_body_it_streamed_before():
    bodies, _deltas = _stream_bodies(_sse_ok)
    assert bodies[0] == {
        "model": "m",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "stream_options": {"include_usage": True},
    }


def test_broker_stream_carries_response_format_through_every_hop(tmp_path):
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return _ok_sse("hi")(request)

    async def run():
        broker = await _streaming_broker(tmp_path, handler, "a")
        async with broker:
            assert [d async for d in broker.stream("hi", response_format=_SCHEMA)] == ["hi"]
            assert [d async for d in broker.llms.stream("hi", response_format=_SCHEMA)] == ["hi"]
            assert [d async for d in broker.stream("hi")] == ["hi"]

    asyncio.run(run())
    assert [body.get("response_format") for body in bodies] == [_SCHEMA, _SCHEMA, None]

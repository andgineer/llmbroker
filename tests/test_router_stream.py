"""Pool streaming: failover before the first delta, no failover after it."""

import asyncio
from contextlib import aclosing

import httpx
import pytest

from llmbroker.broker import router as router_module
from llmbroker.broker.broker import AsyncBroker
from llmbroker.broker.pool import LLMPool
from llmbroker.broker.router import Router
from llmbroker.direct import AsyncDirectClient
from llmbroker.exceptions import (
    InvalidProviderResponseError,
    NoLLMAvailableError,
    StreamInterruptedError,
)
from llmbroker.models import CallStatus, LifecyclePhase, LLMConfig
from llmbroker.standalone.registry import Registry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import FileStore


class _RecordingStore:
    def __init__(self) -> None:
        self.calls: list = []

    async def record(self, call):
        self.calls.append(call)

    async def record_quality(self, llm_name, operation, score, *, call_id=None):
        pass


def _cfg(name: str, host: str) -> LLMConfig:
    return LLMConfig(name=name, base_url=f"https://{host}/v1", model="m", api_key_ref="K")


async def _pool(*cfgs: LLMConfig) -> LLMPool:
    pool = LLMPool()
    for cfg in cfgs:
        await pool.add(cfg, "secret")
    return pool


def _sse(*deltas: str) -> bytes:
    body = b"".join(
        b'data: {"choices": [{"delta": {"content": "%s"}}]}\n\n' % d.encode() for d in deltas
    )
    return body + b'data: {"choices": [], "usage": {"total_tokens": 7}}\n\n' + b"data: [DONE]\n\n"


def _mount(router: Router, handler) -> None:
    router._http = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)  # noqa: SLF001


async def _drain(router: Router, **kwargs) -> list[str]:
    return [d async for d in router.stream([{"role": "user", "content": "hi"}], **kwargs)]


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #


def test_stream_yields_deltas_and_journals_one_ok_row():
    store = _RecordingStore()

    async def run():
        pool = await _pool(_cfg("a", "a"))
        router = Router(pool, store, scope=None)
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
        router = Router(pool, _RecordingStore(), scope=None)
        _mount(
            router,
            lambda _r: httpx.Response(
                200, content=body(), headers={"content-type": "text/event-stream"}
            ),
        )
        seen: list[str] = []
        async for delta in router.stream([{"role": "user", "content": "hi"}]):
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
        router = Router(pool, store, scope=None)
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
        router = Router(pool, store, scope=None)
        _mount(router, handler)
        return await _drain(router)

    assert asyncio.run(run()) == ["ok"]
    assert [c.llm_name for c in store.calls] == ["a", "b"]


def test_stream_raises_when_every_candidate_fails():
    async def run():
        pool = await _pool(_cfg("a", "a"), _cfg("b", "b"))
        router = Router(pool, _RecordingStore(), scope=None)
        _mount(router, lambda _r: httpx.Response(503, text="down"))
        with pytest.raises(NoLLMAvailableError):
            await _drain(router, wait=0.5)

    asyncio.run(run())


def test_wait_bounds_time_to_first_delta_without_blaming_the_model():
    """A budget spent waiting for the first delta is the caller's clock, not a fault:
    timeout error, no cooldown, and the row carries no cooldown_until."""
    store = _RecordingStore()

    async def body():
        await asyncio.sleep(5)
        yield b'data: {"choices": [{"delta": {"content": "late"}}]}\n\n'

    async def run():
        pool = await _pool(_cfg("a", "a"))
        router = Router(pool, store, scope=None)
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
    assert pool.state("a").phase is LifecyclePhase.AVAILABLE  # not cooled
    (row,) = store.calls
    assert (row.status, row.cooldown_until) == (CallStatus.ERROR, None)


def test_wait_does_not_bound_a_slow_consumer():
    """Once deltas flow, the budget is spent: a consumer that dawdles between them
    must not kill its own stream."""

    async def run():
        pool = await _pool(_cfg("a", "a"))
        router = Router(pool, _RecordingStore(), scope=None)
        _mount(
            router,
            lambda _r: httpx.Response(
                200,
                content=_sse("one", "two"),
                headers={"content-type": "text/event-stream"},
            ),
        )
        seen: list[str] = []
        async for delta in router.stream([{"role": "user", "content": "hi"}], wait=0.3):
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
        router = Router(pool, store, scope=None)
        _mount(
            router,
            lambda _r: httpx.Response(
                200, content=body(), headers={"content-type": "text/event-stream"}
            ),
        )
        seen: list[str] = []
        with pytest.raises(StreamInterruptedError) as excinfo:
            async for delta in router.stream([{"role": "user", "content": "hi"}]):
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
        router = Router(pool, store, scope=None)
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
        router = Router(pool, store, scope=None)
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
        router = Router(pool, store, scope=None)
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
        router = Router(pool, store, scope=None)
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
        router = Router(pool, store, scope=None)
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


def test_stream_reraises_the_provider_error_when_every_candidate_rejects_the_request():
    """A 4xx is the request's fault on the streaming path too: nothing is cooled, and
    the provider's own error beats a generic `NoLLMAvailableError`."""
    store = _RecordingStore()

    async def run():
        pool = await _pool(_cfg("a", "a"), _cfg("b", "b"))
        router = Router(pool, store, scope=None)
        _mount(router, lambda _r: httpx.Response(400, text="messages[0].role is invalid"))
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            await _drain(router)
        return exc_info.value, pool

    err, pool = asyncio.run(run())
    assert err.response.status_code == 400  # noqa: PLR2004
    assert [(c.llm_name, c.status) for c in store.calls] == [
        ("a", CallStatus.ERROR),
        ("b", CallStatus.ERROR),
    ]
    assert all(c.cooldown_until is None for c in store.calls)
    assert pool.state("a").phase is LifecyclePhase.AVAILABLE
    assert pool.state("b").phase is LifecyclePhase.AVAILABLE


def test_empty_completion_is_a_success_not_a_garbage_200():
    """A model that answers with nothing said nothing wrong: chunks carrying
    `choices` make it a real completion, however empty."""
    store = _RecordingStore()

    async def run():
        pool = await _pool(_cfg("a", "a"), _cfg("b", "b"))
        router = Router(pool, store, scope=None)
        _mount(
            router,
            lambda _r: httpx.Response(
                200,
                content=b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\n'
                b"data: [DONE]\n\n",
                headers={"content-type": "text/event-stream"},
            ),
        )
        return await _drain(router), pool

    deltas, pool = asyncio.run(run())
    assert deltas == []
    assert [(c.llm_name, c.status) for c in store.calls] == [("a", CallStatus.OK)]
    assert pool.state("a").phase is LifecyclePhase.AVAILABLE


def test_abandoned_stream_releases_the_slot():
    """A consumer that breaks out must not cost the model a unit of `parallel`."""
    store = _RecordingStore()

    async def run():
        cfg = LLMConfig(name="a", base_url="https://a/v1", model="m", api_key_ref="K", parallel=1)
        pool = await _pool(cfg)
        router = Router(pool, store, scope=None)
        _mount(
            router,
            lambda _r: httpx.Response(
                200,
                content=_sse("one", "two", "three"),
                headers={"content-type": "text/event-stream"},
            ),
        )
        async for delta in router.stream([{"role": "user", "content": "hi"}]):
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
        router = Router(pool, store, scope=None)
        _mount(
            router,
            lambda _r: httpx.Response(
                200,
                content=_sse("one", "two", "three"),
                headers={"content-type": "text/event-stream"},
            ),
        )
        async with aclosing(router.stream([{"role": "user", "content": "hi"}])) as deltas:
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
            broker._router._http = httpx.AsyncClient(  # noqa: SLF001
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

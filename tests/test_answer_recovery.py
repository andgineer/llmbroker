"""Continuation over complete answers retained by one streamed pool call."""

import asyncio
from contextlib import aclosing

import httpx
import pytest

from llmbroker.broker.pool import LLMPool
from llmbroker.broker.result import CallReceipt
from llmbroker.broker.router import Router
from llmbroker.exceptions import NoLLMAvailableError, StreamReplacementError
from llmbroker.models import CallStatus, LLMConfig

from support import make_ring


class _RecordingStore:
    def __init__(self) -> None:
        self.calls: list = []

    async def record(self, call) -> None:
        self.calls.append(call)

    async def record_quality(self, call_id, score, *, scope=None) -> None:
        pass


def _cfg(name: str) -> LLMConfig:
    return LLMConfig(name=name, base_url=f"https://{name}/v1", model="m", api_key_ref="K")


async def _pool(*names: str) -> LLMPool:
    pool = LLMPool()
    for order, name in enumerate(names):
        await pool.add(_cfg(name), order)
    return pool


def _sse(*deltas: str) -> bytes:
    chunks = b"".join(
        b'data: {"choices": [{"delta": {"content": "%s"}}]}\n\n' % delta.encode()
        for delta in deltas
    )
    return chunks + b"data: [DONE]\n\n"


def _delta(text: str) -> bytes:
    return b'data: {"choices": [{"delta": {"content": "%s"}}]}\n\n' % text.encode()


def _mount(router: Router, handler) -> None:
    router._http_client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(handler),
        timeout=2.0,
    )


def _stream(router: Router, **kwargs):
    return router.stream(
        make_ring(),
        [{"role": "user", "content": "hi"}],
        CallReceipt(),
        **kwargs,
    )


def test_retained_answers_then_untried_candidate_follow_completion_order():
    async def run():
        gates = {name: asyncio.Event() for name in "abc"}
        opened: list[str] = []
        all_open = asyncio.Event()

        async def body(name: str):
            opened.append(name)
            if len(opened) == 3:  # noqa: PLR2004
                all_open.set()
            yield _sse(name)[:-14]
            await gates[name].wait()
            yield b"data: [DONE]\n\n"

        def handler(request: httpx.Request) -> httpx.Response:
            name = request.url.host or ""
            if name == "d":
                opened.append(name)
            content = _sse(name) if name == "d" else body(name)
            return httpx.Response(
                200,
                content=content,
                headers={"content-type": "text/event-stream"},
            )

        router = Router(await _pool("a", "b", "c", "d"), _RecordingStore())
        _mount(router, handler)
        stream = _stream(router, fastest_of=3, stream_selection_window=0)
        async with aclosing(stream):
            initial = asyncio.create_task(_collect_initial(stream))
            await asyncio.wait_for(all_open.wait(), timeout=1.0)
            gates["b"].set()
            first = await asyncio.wait_for(initial, timeout=1.0)
            gates["c"].set()
            second = await asyncio.wait_for(stream.another(), timeout=1.0)
            gates["a"].set()
            third = await asyncio.wait_for(stream.another(), timeout=1.0)
            fourth = await asyncio.wait_for(stream.another(), timeout=1.0)
            exhausted = await stream.another()
        return first, second, third, fourth, exhausted, opened

    async def _collect_initial(stream):
        try:
            return "".join([delta async for delta in stream])
        except StreamReplacementError as exc:
            return exc.replacement.text

    first, second, third, fourth, exhausted, opened = asyncio.run(run())
    assert [first, second.text, third.text, fourth.text] == ["b", "c", "a", "d"]
    assert exhausted is None
    assert opened == ["a", "b", "c", "d"]


@pytest.mark.parametrize("fastest_of", [None, 1])
def test_width_one_opens_no_new_candidate_until_another(fastest_of):
    opened: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.host or ""
        opened.append(name)
        return httpx.Response(
            200,
            content=_sse(name),
            headers={"content-type": "text/event-stream"},
        )

    async def run():
        router = Router(await _pool("a", "b"), _RecordingStore())
        _mount(router, handler)
        stream = _stream(router, fastest_of=fastest_of)
        async with aclosing(stream):
            first = "".join([delta async for delta in stream])
            opened_after_first = list(opened)
            second = await stream.another()
            return first, opened_after_first, second

    first, opened_after_first, second = asyncio.run(run())
    assert (first, opened_after_first) == ("a", ["a"])
    assert (second.text, opened) == ("b", ["a", "b"])


def test_close_cancels_an_active_continuation_and_settles_its_lane():
    async def run():
        started = asyncio.Event()
        closed = asyncio.Event()
        never = asyncio.Event()

        async def waiting():
            started.set()
            try:
                await never.wait()
            finally:
                closed.set()
            yield b""  # pragma: no cover

        def handler(request: httpx.Request) -> httpx.Response:
            content = _sse("a") if request.url.host == "a" else waiting()
            return httpx.Response(
                200,
                content=content,
                headers={"content-type": "text/event-stream"},
            )

        pool = await _pool("a", "b")
        store = _RecordingStore()
        router = Router(pool, store)
        _mount(router, handler)
        stream = _stream(router, fastest_of=1)
        assert "".join([delta async for delta in stream]) == "a"
        continuation = asyncio.create_task(stream.another())
        await asyncio.wait_for(started.wait(), timeout=1.0)
        await asyncio.wait_for(stream.aclose(), timeout=1.0)
        with pytest.raises(asyncio.CancelledError):
            await continuation
        await asyncio.wait_for(closed.wait(), timeout=1.0)
        return pool, store

    pool, store = asyncio.run(run())
    assert [pool._slots[name].in_flight for name in ("a", "b")] == [0, 0]  # noqa: SLF001
    assert {row.llm_name: row.status for row in store.calls} == {
        "a": CallStatus.OK,
        "b": CallStatus.SUPERSEDED,
    }


def test_lifecycle_and_repeatable_exhaustion():
    async def run():
        router = Router(await _pool("a", "b"), _RecordingStore())
        _mount(
            router,
            lambda request: httpx.Response(
                200,
                content=_sse(request.url.host or ""),
                headers={"content-type": "text/event-stream"},
            ),
        )
        stream = _stream(router, fastest_of=1)
        with pytest.raises(RuntimeError, match="complete initial"):
            await stream.another()
        async with aclosing(stream):
            assert "".join([delta async for delta in stream]) == "a"
            assert (await stream.another()).text == "b"
            assert await stream.another() is None
            assert await stream.another() is None
        with pytest.raises(RuntimeError, match="closed"):
            await stream.another()
        await stream.aclose()

    asyncio.run(run())


def test_typed_initial_replacement_keeps_the_provisional_answer_for_another():
    async def run():
        a_emitted = asyncio.Event()
        finish_a = asyncio.Event()

        async def provisional():
            yield _delta("a")
            a_emitted.set()
            await finish_a.wait()
            yield b"data: [DONE]\n\n"

        async def winner():
            await a_emitted.wait()
            yield _sse("b")

        def handler(request: httpx.Request) -> httpx.Response:
            content = provisional() if request.url.host == "a" else winner()
            return httpx.Response(
                200,
                content=content,
                headers={"content-type": "text/event-stream"},
            )

        router = Router(await _pool("a", "b"), _RecordingStore())
        _mount(router, handler)
        stream = _stream(router, fastest_of=2, stream_selection_window=0)
        async with aclosing(stream):
            with pytest.raises(StreamReplacementError) as caught:
                _ = [delta async for delta in stream]
            finish_a.set()
            another = await stream.another()
            return caught.value.replacement, another

    initial, another = asyncio.run(run())
    assert (initial.text, initial.llm_name) == ("b", "b")
    assert (another.text, another.llm_name) == ("a", "a")


def test_a_slow_earlier_journal_row_cannot_reorder_continuations():
    class _HeldStore(_RecordingStore):
        def __init__(self) -> None:
            super().__init__()
            self.b_settling = asyncio.Event()
            self.release_b = asyncio.Event()

        async def record(self, call) -> None:
            if call.llm_name == "b" and call.status is CallStatus.OK:
                self.b_settling.set()
                await self.release_b.wait()
            await super().record(call)

    async def run():
        gates = {name: asyncio.Event() for name in "abc"}
        opened: set[str] = set()
        all_open = asyncio.Event()

        async def body(name: str):
            opened.add(name)
            if len(opened) == 3:  # noqa: PLR2004
                all_open.set()
            yield _delta(name)
            await gates[name].wait()
            yield b"data: [DONE]\n\n"

        def handler(request: httpx.Request) -> httpx.Response:
            name = request.url.host or ""
            return httpx.Response(
                200,
                content=body(name),
                headers={"content-type": "text/event-stream"},
            )

        store = _HeldStore()
        router = Router(await _pool("a", "b", "c"), store)
        _mount(router, handler)
        stream = _stream(router, fastest_of=3, stream_selection_window=0)
        async with aclosing(stream):
            initial = asyncio.create_task(_collect(stream))
            await asyncio.wait_for(all_open.wait(), timeout=1.0)
            gates["a"].set()
            assert await asyncio.wait_for(initial, timeout=1.0) == "a"
            gates["b"].set()
            await asyncio.wait_for(store.b_settling.wait(), timeout=1.0)
            gates["c"].set()
            await asyncio.sleep(0)
            next_answer = asyncio.create_task(stream.another())
            await asyncio.sleep(0)
            assert not next_answer.done()
            store.release_b.set()
            second = await asyncio.wait_for(next_answer, timeout=1.0)
            third = await asyncio.wait_for(stream.another(), timeout=1.0)
            return second, third

    async def _collect(stream) -> str:
        return "".join([delta async for delta in stream])

    second, third = asyncio.run(run())
    assert (second.text, third.text) == ("b", "c")


def test_a_live_handle_does_not_admit_models_added_after_initial_membership():
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.host or "")
        return httpx.Response(
            200,
            content=_sse(request.url.host or ""),
            headers={"content-type": "text/event-stream"},
        )

    async def run():
        pool = await _pool("a")
        router = Router(pool, _RecordingStore())
        _mount(router, handler)
        stream = _stream(router)
        async with aclosing(stream):
            assert "".join([delta async for delta in stream]) == "a"
            await pool.add(_cfg("b"), 1)
            return await stream.another()

    assert asyncio.run(run()) is None
    assert requested == ["a"]


def test_continuation_absorbs_a_provider_failure_while_a_candidate_can_answer():
    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.host or ""
        if name == "b":
            return httpx.Response(429, text="later")
        return httpx.Response(
            200,
            content=_sse(name),
            headers={"content-type": "text/event-stream"},
        )

    async def run():
        store = _RecordingStore()
        router = Router(await _pool("a", "b", "c"), store)
        _mount(router, handler)
        stream = _stream(router, fastest_of=1)
        async with aclosing(stream):
            assert "".join([delta async for delta in stream]) == "a"
            answer = await stream.another()
        return answer, store.calls

    answer, calls = asyncio.run(run())
    assert (answer.text, answer.llm_name) == ("c", "c")
    assert [(call.llm_name, call.status) for call in calls] == [
        ("a", CallStatus.OK),
        ("b", CallStatus.RATE_LIMITED),
        ("c", CallStatus.OK),
    ]


def test_close_before_start_opens_no_provider_and_writes_no_row():
    requested: list[str] = []

    async def run():
        store = _RecordingStore()
        router = Router(await _pool("a"), store)
        _mount(router, lambda request: requested.append(request.url.host or ""))
        stream = _stream(router)
        await stream.aclose()
        return store.calls

    assert asyncio.run(run()) == []
    assert requested == []


def test_negative_wait_opens_no_initial_provider():
    requested: list[str] = []

    async def run():
        router = Router(await _pool("a"), _RecordingStore())

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(request.url.host or "")
            return httpx.Response(
                200, content=_sse("a"), headers={"content-type": "text/event-stream"}
            )

        _mount(router, handler)
        stream = _stream(router, wait=-1)
        async with aclosing(stream):
            with pytest.raises(NoLLMAvailableError) as caught:
                _ = [delta async for delta in stream]
        return caught.value

    error = asyncio.run(run())
    assert isinstance(error, NoLLMAvailableError)
    assert requested == []


def test_continuation_waits_for_a_busy_untried_candidate_within_original_budget():
    async def run():
        pool = await _pool("a", "b")
        router = Router(pool, _RecordingStore())
        _mount(
            router,
            lambda request: httpx.Response(
                200,
                content=_sse(request.url.host or ""),
                headers={"content-type": "text/event-stream"},
            ),
        )
        stream = _stream(router, wait=0.5)
        async with aclosing(stream):
            assert "".join([delta async for delta in stream]) == "a"
            busy = await pool.acquire(
                None,
                payable=frozenset({"K"}),
                exclude=frozenset({"a"}),
            )
            continuation = asyncio.create_task(stream.another())
            await asyncio.sleep(0)
            assert not continuation.done()
            await pool.release(busy)
            return await asyncio.wait_for(continuation, timeout=1.0)

    answer = asyncio.run(run())
    assert (answer.text, answer.llm_name) == ("b", "b")

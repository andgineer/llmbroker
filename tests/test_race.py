"""Executable contract for explicit parallel answers and protected recovery."""

import asyncio
import inspect
from contextlib import aclosing
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
import pytest

from llmbroker.broker.broker import AsyncBroker
from llmbroker.broker.learning import Learner
from llmbroker.broker.llms import AsyncLLMs
from llmbroker.broker.pool import LLMPool
from llmbroker.broker.result import CallReceipt
from llmbroker.broker.router import Router
from llmbroker.exceptions import StreamInterruptedError
from llmbroker.models import CallStatus, LifecyclePhase, LLMConfig
from llmbroker.optimizer import Optimizer
from llmbroker.sync import Broker, LLMs

from support import make_ring

pytestmark = pytest.mark.skip(
    reason="executable contract for queued parallel routing; remove before implementation",
)

_PATCH = "llmbroker.broker.router.call_provider"


class _RecordingStore:
    def __init__(self) -> None:
        self.calls: list = []

    async def record(self, call):
        self.calls.append(call)

    async def record_quality(self, call_id, score, *, scope=None):
        pass


def _cfg(name: str, *, parallel: int | None = None) -> LLMConfig:
    return LLMConfig(
        name=name,
        base_url=f"https://{name}/v1",
        model="m",
        api_key_ref="K",
        parallel=parallel,
    )


async def _pool(*names: str, optimizer: Optimizer | None = None) -> LLMPool:
    pool = LLMPool(optimizer=optimizer)
    for order, name in enumerate(names):
        await pool.add(_cfg(name), order)
    return pool


async def _expire_cooldown(pool: LLMPool, name: str) -> None:
    acquired = await pool.acquire(None, payable=frozenset({"K"}))
    assert acquired.name == name
    await pool.cool_down(acquired, 60)
    pool._slots[name].cooldown_until = datetime.now(UTC) - timedelta(seconds=1)  # noqa: SLF001


def _sse(*deltas: str) -> bytes:
    chunks = b"".join(
        b'data: {"choices": [{"delta": {"content": "%s"}}]}\n\n' % delta.encode()
        for delta in deltas
    )
    return chunks + b"data: [DONE]\n\n"


def _mount(router: Router, handler) -> None:
    router._http_client = httpx.AsyncClient(  # noqa: SLF001
        transport=httpx.MockTransport(handler),
        timeout=2.0,
    )


def _stream(router: Router, receipt: CallReceipt | None = None, **kwargs):
    return router.stream(
        make_ring(),
        [{"role": "user", "content": "hi"}],
        receipt or CallReceipt(),
        **kwargs,
    )


async def _drain(router: Router, **kwargs) -> list[str]:
    return [delta async for delta in _stream(router, **kwargs)]


def test_public_routed_surfaces_expose_parallel_options_and_direct_does_not():
    routed = (
        AsyncBroker.ask,
        AsyncBroker.chat,
        AsyncBroker.stream,
        AsyncLLMs.ask,
        AsyncLLMs.chat,
        AsyncLLMs.stream,
        Broker.ask,
        Broker.chat,
        LLMs.ask,
        LLMs.chat,
    )
    for method in routed:
        parameters = inspect.signature(method).parameters
        assert "fastest_of" in parameters
        assert "parallel_recovery" in parameters
    direct = inspect.signature(AsyncBroker.direct).parameters
    assert "fastest_of" not in direct
    assert "parallel_recovery" not in direct


@pytest.mark.parametrize("kwargs", [{}, {"fastest_of": None}, {"fastest_of": 1}])
def test_healthy_route_stays_single_lane_with_recovery_enabled_by_default(kwargs):
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.host or "")
        return httpx.Response(
            200,
            content=_sse(request.url.host or ""),
            headers={"content-type": "text/event-stream"},
        )

    async def run():
        router = Router(await _pool("a", "b"), _RecordingStore())
        _mount(router, handler)
        return await _drain(router, **kwargs)

    assert asyncio.run(run()) == ["a"]
    assert requested == ["a"]


@pytest.mark.parametrize("kwargs", [{}, {"fastest_of": None}, {"fastest_of": 1}])
def test_recovery_is_parallel_by_default_and_the_first_delta_wins(kwargs):
    requested: list[str] = []

    async def run():
        recovery_started = asyncio.Event()
        recovery_closed = asyncio.Event()
        never = asyncio.Event()

        async def recovery_body():
            recovery_started.set()
            try:
                await never.wait()
            finally:
                recovery_closed.set()
            yield b""  # pragma: no cover - keeps this an async iterator

        async def ordinary_body():
            await recovery_started.wait()
            yield _sse("b-wins")

        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host or ""
            requested.append(host)
            body = recovery_body() if host == "a" else ordinary_body()
            return httpx.Response(
                200,
                content=body,
                headers={"content-type": "text/event-stream"},
            )

        pool = await _pool("a", "b")
        await _expire_cooldown(pool, "a")
        router = Router(pool, _RecordingStore())
        _mount(router, handler)
        deltas = await asyncio.wait_for(_drain(router, **kwargs), timeout=1.0)
        await asyncio.wait_for(recovery_closed.wait(), timeout=1.0)
        return deltas

    assert asyncio.run(run()) == ["b-wins"]
    assert set(requested) == {"a", "b"}


def test_parallel_recovery_false_puts_the_recovery_attempt_back_on_the_call_path():
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.host or "")
        return httpx.Response(
            200,
            content=_sse(request.url.host or ""),
            headers={"content-type": "text/event-stream"},
        )

    async def run():
        pool = await _pool("a", "b")
        await _expire_cooldown(pool, "a")
        router = Router(pool, _RecordingStore())
        _mount(router, handler)
        return await _drain(router, parallel_recovery=False)

    assert asyncio.run(run()) == ["a"]
    assert requested == ["a"]


def test_recovery_success_returns_following_calls_to_one_lane():
    requested: list[str] = []

    async def run():
        backup_started = asyncio.Event()
        never = asyncio.Event()

        async def recovery_body():
            await backup_started.wait()
            yield _sse("a-wins")

        async def backup_body():
            backup_started.set()
            await never.wait()
            yield b""  # pragma: no cover

        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host or ""
            requested.append(host)
            body = recovery_body() if host == "a" else backup_body()
            return httpx.Response(
                200,
                content=body,
                headers={"content-type": "text/event-stream"},
            )

        pool = await _pool("a", "b")
        await _expire_cooldown(pool, "a")
        router = Router(pool, _RecordingStore())
        _mount(router, handler)
        first = await asyncio.wait_for(_drain(router), timeout=1.0)
        second = await asyncio.wait_for(_drain(router), timeout=1.0)
        return first, second

    assert asyncio.run(run()) == (["a-wins"], ["a-wins"])
    assert requested.count("a") == 2
    assert requested.count("b") == 1


def test_superseded_recovery_is_tried_with_a_parallel_candidate_again():
    requested: list[str] = []

    async def run():
        recovery_started = asyncio.Event()
        never = asyncio.Event()

        async def recovery_body():
            recovery_started.set()
            await never.wait()
            yield b""  # pragma: no cover

        async def winner_body():
            await recovery_started.wait()
            yield _sse("b-wins")

        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host or ""
            requested.append(host)
            body = recovery_body() if host == "a" else winner_body()
            return httpx.Response(
                200,
                content=body,
                headers={"content-type": "text/event-stream"},
            )

        pool = await _pool("a", "b")
        await _expire_cooldown(pool, "a")
        router = Router(pool, _RecordingStore())
        _mount(router, handler)
        first = await asyncio.wait_for(_drain(router), timeout=1.0)
        second = await asyncio.wait_for(_drain(router), timeout=1.0)
        return first, second

    assert asyncio.run(run()) == (["b-wins"], ["b-wins"])
    assert requested.count("a") == 2
    assert requested.count("b") == 2


def test_concurrent_callers_claim_one_post_cooldown_recovery():
    requested: list[str] = []

    async def run():
        first_pair_started = asyncio.Event()
        release_first_pair = asyncio.Event()
        starts = 0

        async def held_body(host: str):
            nonlocal starts
            starts += 1
            if starts == 2:
                first_pair_started.set()
            await release_first_pair.wait()
            yield _sse(host)

        async def immediate_body(host: str):
            yield _sse(host)

        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host or ""
            requested.append(host)
            body = held_body(host) if host in {"a", "b"} else immediate_body(host)
            return httpx.Response(
                200,
                content=body,
                headers={"content-type": "text/event-stream"},
            )

        pool = LLMPool()
        await pool.add(_cfg("a"), 0)
        for order, name in enumerate(("b", "c", "d"), start=1):
            await pool.add(_cfg(name, parallel=1), order)
        await _expire_cooldown(pool, "a")
        router = Router(pool, _RecordingStore())
        _mount(router, handler)

        first = asyncio.create_task(_drain(router))
        await asyncio.wait_for(first_pair_started.wait(), timeout=1.0)
        second = await asyncio.wait_for(_drain(router), timeout=1.0)
        release_first_pair.set()
        await asyncio.wait_for(first, timeout=1.0)
        return second

    assert asyncio.run(run()) == ["c"]
    assert requested.count("a") == 1
    assert requested.count("b") == 1
    assert requested.count("c") == 1
    assert "d" not in requested


def test_fastest_of_starts_distinct_models_and_first_delta_wins():
    requested: list[str] = []

    async def run():
        slow_started = asyncio.Event()
        slow_closed = asyncio.Event()
        never = asyncio.Event()

        async def slow_body():
            slow_started.set()
            try:
                await never.wait()
            finally:
                slow_closed.set()
            yield b""  # pragma: no cover - keeps this an async iterator

        async def fast_body():
            await slow_started.wait()
            yield _sse("b-first", "b-rest")

        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host or ""
            requested.append(host)
            body = slow_body() if host == "a" else fast_body()
            return httpx.Response(
                200,
                content=body,
                headers={"content-type": "text/event-stream"},
            )

        router = Router(await _pool("a", "b"), _RecordingStore())
        _mount(router, handler)
        deltas = await asyncio.wait_for(
            _drain(router, fastest_of=2, parallel_recovery=False),
            timeout=1.0,
        )
        await asyncio.wait_for(slow_closed.wait(), timeout=1.0)
        return deltas

    assert asyncio.run(run()) == ["b-first", "b-rest"]
    assert set(requested) == {"a", "b"}


def test_fastest_of_with_parallel_recovery_warns_but_does_not_add_a_lane(caplog):
    requested: list[str] = []

    async def run():
        recovery_started = asyncio.Event()
        never = asyncio.Event()

        async def recovery_body():
            recovery_started.set()
            await never.wait()
            yield b""  # pragma: no cover

        async def winner_body():
            await recovery_started.wait()
            yield _sse("b-wins")

        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host or ""
            requested.append(host)
            body = recovery_body() if host == "a" else winner_body()
            return httpx.Response(
                200,
                content=body,
                headers={"content-type": "text/event-stream"},
            )

        pool = await _pool("a", "b", "c")
        await _expire_cooldown(pool, "a")
        router = Router(pool, _RecordingStore())
        _mount(router, handler)
        with caplog.at_level("WARNING", logger="llmbroker.broker"):
            return await asyncio.wait_for(
                _drain(router, fastest_of=2, parallel_recovery=True),
                timeout=1.0,
            )

    assert asyncio.run(run()) == ["b-wins"]
    assert set(requested) == {"a", "b"}
    assert "fastest_of" in caplog.text
    assert "parallel_recovery" in caplog.text


def test_stream_commits_to_the_first_delta_and_never_splices_the_loser():
    async def run():
        release_winner = asyncio.Event()
        loser_closed = asyncio.Event()

        async def winner_body():
            yield _sse("a-first")[:-14]
            await release_winner.wait()
            raise httpx.ReadError("winner died")

        async def loser_body():
            try:
                await asyncio.sleep(0.05)
                yield _sse("b-answer")
            finally:
                loser_closed.set()

        def handler(request: httpx.Request) -> httpx.Response:
            body = winner_body() if request.url.host == "a" else loser_body()
            return httpx.Response(
                200,
                content=body,
                headers={"content-type": "text/event-stream"},
            )

        router = Router(await _pool("a", "b"), _RecordingStore())
        _mount(router, handler)
        seen: list[str] = []
        with pytest.raises(StreamInterruptedError):
            async with aclosing(_stream(router, fastest_of=2, parallel_recovery=False)) as deltas:
                async for delta in deltas:
                    seen.append(delta)
                    release_winner.set()
        await asyncio.wait_for(loser_closed.wait(), timeout=1.0)
        return seen

    assert asyncio.run(run()) == ["a-first"]


def test_superseded_stream_lane_is_journaled_and_neutral_to_learning():
    async def run():
        loser_started = asyncio.Event()
        never = asyncio.Event()

        async def loser_body():
            loser_started.set()
            await never.wait()
            yield b""  # pragma: no cover

        async def winner_body():
            await loser_started.wait()
            yield _sse("ok")

        def handler(request: httpx.Request) -> httpx.Response:
            body = loser_body() if request.url.host == "a" else winner_body()
            return httpx.Response(
                200,
                content=body,
                headers={"content-type": "text/event-stream"},
            )

        optimizer = Optimizer()
        pool = await _pool("a", "b", optimizer=optimizer)
        store = _RecordingStore()
        router = Router(pool, store, optimizer=optimizer, learner=Learner(optimizer, store, pool))
        _mount(router, handler)
        assert await asyncio.wait_for(
            _drain(router, fastest_of=2, parallel_recovery=False),
            timeout=1.0,
        ) == ["ok"]
        return pool, optimizer, store

    pool, optimizer, store = asyncio.run(run())
    rows = {row.llm_name: row for row in store.calls}
    assert rows["a"].status is CallStatus.SUPERSEDED
    assert rows["a"].cooldown_until is None
    assert rows["a"].budget_ms is None
    assert pool.state("a").phase is LifecyclePhase.AVAILABLE
    assert optimizer.rl_fail_count("a") == 0
    assert "a" not in pool._budget_bounds  # noqa: SLF001
    assert rows["b"].status is CallStatus.OK


def test_real_failure_cools_its_lane_and_replenishes_from_an_untried_model():
    requested: list[str] = []

    async def run():
        b_started = asyncio.Event()
        never = asyncio.Event()

        async def b_body():
            b_started.set()
            await never.wait()
            yield b""  # pragma: no cover

        async def c_body():
            await b_started.wait()
            yield _sse("c-wins")

        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host or ""
            requested.append(host)
            if host == "a":
                return httpx.Response(503, text="down")
            body = b_body() if host == "b" else c_body()
            return httpx.Response(
                200,
                content=body,
                headers={"content-type": "text/event-stream"},
            )

        pool = await _pool("a", "b", "c")
        store = _RecordingStore()
        router = Router(pool, store)
        _mount(router, handler)
        result = await asyncio.wait_for(
            _drain(router, fastest_of=2, parallel_recovery=False),
            timeout=1.0,
        )
        return result, pool, store

    result, pool, store = asyncio.run(run())
    assert result == ["c-wins"]
    assert set(requested) == {"a", "b", "c"}
    rows = {row.llm_name: row for row in store.calls}
    assert rows["a"].status is CallStatus.UNAVAILABLE
    assert rows["a"].cooldown_until is not None
    assert pool.state("a").phase is LifecyclePhase.COOLING
    assert rows["b"].status is CallStatus.SUPERSEDED
    assert rows["c"].status is CallStatus.OK


def test_chat_returns_the_first_complete_answer_and_cancels_the_loser():
    async def run():
        slow_started = asyncio.Event()
        slow_cancelled = asyncio.Event()
        never = asyncio.Event()

        async def provider(config, *_args, **_kwargs):
            if config.name == "a":
                slow_started.set()
                try:
                    await never.wait()
                finally:
                    slow_cancelled.set()
                return "late", None, None
            await slow_started.wait()
            return "fast", None, None

        pool = await _pool("a", "b")
        store = _RecordingStore()
        router = Router(pool, store)
        with patch(_PATCH, new=provider):
            answer = await asyncio.wait_for(
                router.chat(
                    make_ring(),
                    [{"role": "user", "content": "hi"}],
                    fastest_of=2,
                    parallel_recovery=False,
                ),
                timeout=1.0,
            )
        await asyncio.wait_for(slow_cancelled.wait(), timeout=1.0)
        return answer, store

    answer, store = asyncio.run(run())
    assert (answer.text, answer.llm_name) == ("fast", "b")
    assert {row.llm_name: row.status for row in store.calls} == {
        "a": CallStatus.SUPERSEDED,
        "b": CallStatus.OK,
    }


@pytest.mark.parametrize("fastest_of", [True, False, 0, -1])
def test_invalid_fastest_of_opens_no_provider_request(fastest_of):
    async def run():
        router = Router(await _pool("a"), _RecordingStore())
        called = False

        async def provider(*_args, **_kwargs):
            nonlocal called
            called = True
            return "unexpected", None, None

        with patch(_PATCH, new=provider), pytest.raises(ValueError, match="fastest_of"):
            await router.chat(
                make_ring(),
                [{"role": "user", "content": "hi"}],
                fastest_of=fastest_of,
            )
        return called

    assert asyncio.run(run()) is False


@pytest.mark.parametrize("parallel_recovery", [None, 0, 1, "yes"])
def test_invalid_parallel_recovery_opens_no_provider_request(parallel_recovery):
    async def run():
        router = Router(await _pool("a"), _RecordingStore())
        called = False

        async def provider(*_args, **_kwargs):
            nonlocal called
            called = True
            return "unexpected", None, None

        with (
            patch(_PATCH, new=provider),
            pytest.raises(
                ValueError,
                match="parallel_recovery",
            ),
        ):
            await router.chat(
                make_ring(),
                [{"role": "user", "content": "hi"}],
                parallel_recovery=parallel_recovery,
            )
        return called

    assert asyncio.run(run()) is False

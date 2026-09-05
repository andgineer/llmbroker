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
from llmbroker.exceptions import NoLLMAvailableError, StreamInterruptedError
from llmbroker.models import CallStatus, LifecyclePhase, LLMConfig
from llmbroker.optimizer import Optimizer
from llmbroker.sync import Broker, LLMs

from support import make_ring

_PATCH = "llmbroker.broker.router.call_provider"


class _RecordingStore:
    def __init__(self) -> None:
        self.calls: list = []

    async def record(self, call):
        self.calls.append(call)

    async def record_quality(self, call_id, score, *, scope=None):
        pass


class _GatedStore(_RecordingStore):
    """A store whose superseded write hangs until released, standing in for a backend
    slow enough to be felt on the call path."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def record(self, call):
        if call.status is CallStatus.SUPERSEDED:
            await self.release.wait()
        await super().record(call)


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


async def _due_for_recovery(pool: LLMPool, name: str) -> None:
    await pool.cool_down(pool.config(name), 60)
    pool._slots[name].cooldown_until = datetime.now(UTC) - timedelta(seconds=1)  # noqa: SLF001


async def _expire_cooldown(pool: LLMPool, name: str) -> None:
    acquired = await pool.acquire(None, payable=frozenset({"K"}))
    assert acquired.name == name
    await _due_for_recovery(pool, name)


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


def test_fastest_of_with_default_recovery_adds_no_lane_and_logs_no_warning(caplog):
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
        caplog.clear()
        with caplog.at_level("WARNING", logger="llmbroker.broker"):
            return await asyncio.wait_for(
                _drain(router, fastest_of=2, parallel_recovery=True),
                timeout=1.0,
            )

    assert asyncio.run(run()) == ["b-wins"]
    assert set(requested) == {"a", "b"}
    assert caplog.records == []


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


def test_fastest_of_over_the_eligible_pool_simply_runs_fewer_lanes():
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.host or "")
        return httpx.Response(
            200,
            content=_sse("only"),
            headers={"content-type": "text/event-stream"},
        )

    async def run():
        router = Router(await _pool("a"), _RecordingStore())
        _mount(router, handler)
        return await _drain(router, fastest_of=3, parallel_recovery=False)

    assert asyncio.run(run()) == ["only"]
    assert requested == ["a"]


def test_recovery_never_waits_to_find_an_insurance_candidate():
    """The protection is opportunistic: a busy pool must not hold the recovery attempt
    back until a second lane frees up."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.host or "")
        return httpx.Response(
            200,
            content=_sse(request.url.host or ""),
            headers={"content-type": "text/event-stream"},
        )

    async def run():
        pool = LLMPool()
        await pool.add(_cfg("a"), 0)
        await pool.add(_cfg("b", parallel=1), 1)
        await _expire_cooldown(pool, "a")
        busy = await pool.acquire(None, payable=frozenset({"K"}), exclude=frozenset({"a"}))
        assert busy.name == "b"
        router = Router(pool, _RecordingStore())
        _mount(router, handler)
        return await asyncio.wait_for(_drain(router), timeout=1.0)

    assert asyncio.run(run()) == ["a"]
    assert requested == ["a"]


def test_racing_lanes_share_one_wait_budget():
    async def run():
        never = asyncio.Event()

        async def silent_body():
            await never.wait()
            yield b""  # pragma: no cover

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=silent_body(),
                headers={"content-type": "text/event-stream"},
            )

        pool = await _pool("a", "b")
        store = _RecordingStore()
        router = Router(pool, store)
        _mount(router, handler)
        started = asyncio.get_running_loop().time()
        with pytest.raises(NoLLMAvailableError) as raised:
            await asyncio.wait_for(
                _drain(router, fastest_of=2, parallel_recovery=False, wait=0.15),
                timeout=2.0,
            )
        return raised.value, asyncio.get_running_loop().time() - started, store

    error, elapsed, store = asyncio.run(run())
    assert error.reason == "timeout"
    assert elapsed < 1.0
    assert {row.llm_name for row in store.calls} == {"a", "b"}
    assert all(row.budget_ms is not None and 0 < row.budget_ms <= 150 for row in store.calls)


def test_the_first_delta_does_not_wait_for_a_losing_lane_journal_write():
    """A store slow enough to matter may not hold the answer up: the loser's neutral
    row is written beside the delta the caller is already reading, not in front of it."""

    async def run():
        never = asyncio.Event()

        async def loser_body():
            await never.wait()
            yield b""  # pragma: no cover

        async def winner_body():
            yield _sse("a-first", "a-rest")

        def handler(request: httpx.Request) -> httpx.Response:
            body = winner_body() if request.url.host == "a" else loser_body()
            return httpx.Response(
                200,
                content=body,
                headers={"content-type": "text/event-stream"},
            )

        store = _GatedStore()
        router = Router(await _pool("a", "b"), store)
        _mount(router, handler)
        seen: list[str] = []
        async with aclosing(_stream(router, fastest_of=2, parallel_recovery=False)) as deltas:
            seen.append(await asyncio.wait_for(anext(deltas), timeout=1.0))
            written_by_then = list(store.calls)
            store.release.set()
            async for delta in deltas:
                seen.append(delta)
        return seen, written_by_then, store.calls

    seen, written_by_then, calls = asyncio.run(run())
    assert seen == ["a-first", "a-rest"]
    assert written_by_then == []
    assert {row.llm_name: row.status for row in calls} == {
        "a": CallStatus.OK,
        "b": CallStatus.SUPERSEDED,
    }


def test_a_lane_already_journaling_its_failure_keeps_it_when_a_sibling_wins():
    """Losing the race cancels a lane that is still on the provider, never one already
    settling: a cooldown the journal never heard about is silently lost evidence."""

    async def run():
        journaling = asyncio.Event()
        release = asyncio.Event()

        class _HeldFailure(_RecordingStore):
            async def record(self, call):
                if call.llm_name == "a":
                    journaling.set()
                    await release.wait()
                await super().record(call)

        async def winner_body():
            await journaling.wait()
            yield _sse("b-wins")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "a":
                return httpx.Response(503, text="down")
            return httpx.Response(
                200,
                content=winner_body(),
                headers={"content-type": "text/event-stream"},
            )

        optimizer = Optimizer()
        pool = await _pool("a", "b", optimizer=optimizer)
        store = _HeldFailure()
        router = Router(pool, store, optimizer=optimizer, learner=Learner(optimizer, store, pool))
        _mount(router, handler)
        seen: list[str] = []
        async with aclosing(_stream(router, fastest_of=2, parallel_recovery=False)) as deltas:
            seen.append(await asyncio.wait_for(anext(deltas), timeout=1.0))
            release.set()
            async for delta in deltas:
                seen.append(delta)
        return seen, pool, optimizer, store

    seen, pool, optimizer, store = asyncio.run(run())
    assert seen == ["b-wins"]
    rows = {row.llm_name: row for row in store.calls}
    assert [row.status for row in store.calls if row.llm_name == "a"] == [CallStatus.UNAVAILABLE]
    assert rows["a"].cooldown_until is not None
    assert pool.state("a").phase is LifecyclePhase.COOLING
    assert optimizer.rl_fail_count("a") == 1
    assert rows["b"].status is CallStatus.OK


def test_a_recovery_is_covered_by_an_ordinary_candidate_not_a_second_recheck():
    """Two entries off cooldown at once do not cover each other: the protection exists
    to keep the pool's own uncertainty off the caller's path, not to double it."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        requested.append(host)
        return httpx.Response(
            200,
            content=_sse(host),
            headers={"content-type": "text/event-stream"},
        )

    async def run():
        pool = await _pool("a", "b", "c")
        await _due_for_recovery(pool, "a")
        await _due_for_recovery(pool, "b")
        router = Router(pool, _RecordingStore())
        _mount(router, handler)
        await asyncio.wait_for(_drain(router), timeout=1.0)
        return pool

    pool = asyncio.run(run())
    assert set(requested) == {"a", "c"}
    assert pool._slots["b"].recovery_due  # noqa: SLF001


def test_a_lane_a_bug_hit_keeps_its_error_row_when_a_sibling_wins():
    """An attempt leaves its provider by any route, not only a classified failure: a lane
    journaling an unexpected exception is waited for, or it vanishes with its row."""

    async def run():
        class _SlowError(_RecordingStore):
            async def record(self, call):
                if call.llm_name == "a":
                    await asyncio.sleep(0.2)
                await super().record(call)

        async def provider(config, *_args, **_kwargs):
            if config.name == "a":
                raise ValueError("adapter bug")
            return "fast", None, None

        pool = await _pool("a", "b")
        store = _SlowError()
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
        return answer, store, pool

    answer, store, pool = asyncio.run(run())
    assert (answer.text, answer.llm_name) == ("fast", "b")
    assert {row.llm_name: row.status for row in store.calls} == {
        "a": CallStatus.ERROR,
        "b": CallStatus.OK,
    }
    assert pool.state("a").phase is LifecyclePhase.AVAILABLE
    assert pool._slots["a"].in_flight == 0


def test_a_streaming_lane_a_bug_hit_keeps_its_error_row_when_a_sibling_wins():
    """The same on the streaming side, where the abort path settles the attempt."""

    async def run():
        journaling = asyncio.Event()
        release = asyncio.Event()

        class _HeldError(_RecordingStore):
            async def record(self, call):
                if call.llm_name == "a":
                    journaling.set()
                    await release.wait()
                await super().record(call)

        async def winner_body():
            await journaling.wait()
            yield _sse("b-wins")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "a":
                raise ValueError("adapter bug")
            return httpx.Response(
                200,
                content=winner_body(),
                headers={"content-type": "text/event-stream"},
            )

        pool = await _pool("a", "b")
        store = _HeldError()
        router = Router(pool, store)
        _mount(router, handler)
        seen: list[str] = []
        async with aclosing(_stream(router, fastest_of=2, parallel_recovery=False)) as deltas:
            seen.append(await asyncio.wait_for(anext(deltas), timeout=1.0))
            release.set()
            async for delta in deltas:
                seen.append(delta)
        return seen, store, pool

    seen, store, pool = asyncio.run(run())
    assert seen == ["b-wins"]
    assert {row.llm_name: row.status for row in store.calls} == {
        "a": CallStatus.ERROR,
        "b": CallStatus.OK,
    }
    assert pool.state("a").phase is LifecyclePhase.AVAILABLE
    assert pool._slots["a"].in_flight == 0

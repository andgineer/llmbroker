"""Executable contract for explicit parallel answers and protected recovery."""

import asyncio
import inspect
import time
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
from llmbroker.broker.router import Router, _Outcome, _StreamLane, _StreamRace
from llmbroker.exceptions import (
    NoLLMAvailableError,
    StreamReplacementError,
)
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


def _delta(text: str) -> bytes:
    return b'data: {"choices": [{"delta": {"content": "%s"}}]}\n\n' % text.encode()


_DONE = b"data: [DONE]\n\n"


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


def test_recovery_only_parallelism_still_commits_to_the_first_delta():
    """The narrow protection the pool owns keeps its old settlement: the lane that opens
    first is the answer, and the sibling covering the recheck is dropped unread."""

    async def run():
        never = asyncio.Event()
        cover_closed = asyncio.Event()

        async def recovery_body():
            yield _delta("a-first")
            await never.wait()
            yield _DONE  # pragma: no cover

        async def cover_body():
            try:
                await never.wait()
                yield _DONE  # pragma: no cover
            finally:
                cover_closed.set()

        def handler(request: httpx.Request) -> httpx.Response:
            body = recovery_body() if request.url.host == "a" else cover_body()
            return httpx.Response(
                200,
                content=body,
                headers={"content-type": "text/event-stream"},
            )

        pool = await _pool("a", "b")
        await _expire_cooldown(pool, "a")
        store = _RecordingStore()
        router = Router(pool, store)
        _mount(router, handler)
        receipt = CallReceipt()
        seen: list[str] = []
        async with aclosing(_stream(router, receipt, parallel_recovery=True)) as deltas:
            seen.append(await asyncio.wait_for(anext(deltas), timeout=1.0))
        await asyncio.wait_for(cover_closed.wait(), timeout=1.0)
        return seen, receipt, store

    seen, receipt, store = asyncio.run(run())
    assert seen == ["a-first"]
    assert receipt.llm_name == "a"
    assert {row.llm_name: row.status for row in store.calls} == {
        "a": CallStatus.OK,
        "b": CallStatus.SUPERSEDED,
    }


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
        both_open = asyncio.Event()

        async def loser_body():
            await never.wait()
            yield b""  # pragma: no cover

        async def winner_body():
            await both_open.wait()
            yield _sse("a-first", "a-rest")

        store = _GatedStore()
        router = Router(await _pool("a", "b"), store)
        _mount(router, _paired_handler({"a": winner_body, "b": loser_body}, both_open))
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
    # The winner's own row is what a complete answer settles on; the loser's neutral one
    # is what may not stand between that answer and the caller.
    assert [(row.llm_name, row.status) for row in written_by_then] == [("a", CallStatus.OK)]
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


# ── explicit streaming ``fastest_of``: racing complete answers ───────────────


def _race_handler(bodies: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        return httpx.Response(
            200,
            content=bodies[host](),
            headers={"content-type": "text/event-stream"},
        )

    return handler


def _paired_handler(bodies: dict, both_open: asyncio.Event):
    """A race handler that reports when every lane has opened its request. A winner body
    gated on that cannot finish before its siblings are on their providers — and a lane
    retired before it ever reached one has no row to write beside the answer."""
    opened = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal opened
        opened += 1
        if opened == len(bodies):
            both_open.set()
        host = request.url.host or ""
        return httpx.Response(
            200,
            content=bodies[host](),
            headers={"content-type": "text/event-stream"},
        )

    return handler


def _silent(never: asyncio.Event):
    async def body():
        await never.wait()
        yield _DONE  # pragma: no cover - the lane is cancelled first

    return body


def test_a_raced_stream_starts_every_lane_before_either_may_finish():
    """Both providers are opened at once; neither is allowed to answer until the other
    has been asked, so nothing here can pass on a sequential call path."""
    requested: list[str] = []

    async def run():
        both_open = asyncio.Event()
        opened = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal opened
            host = request.url.host or ""
            requested.append(host)
            opened += 1
            if opened == 2:  # noqa: PLR2004
                both_open.set()

            async def body():
                await both_open.wait()
                yield _sse(f"{host}-answer")

            return httpx.Response(
                200,
                content=body(),
                headers={"content-type": "text/event-stream"},
            )

        router = Router(await _pool("a", "b"), _RecordingStore())
        _mount(router, handler)
        return await asyncio.wait_for(
            _drain(router, fastest_of=2, parallel_recovery=False),
            timeout=2.0,
        )

    deltas = asyncio.run(run())
    assert set(requested) == {"a", "b"}
    assert deltas in (["a-answer"], ["b-answer"])


def test_the_preferred_lane_inside_the_window_is_exposed_at_once():
    """Nothing waits for a reserve once the lane that already has priority has begun."""

    async def run():
        never = asyncio.Event()

        async def preferred():
            yield _delta("a-first")
            await never.wait()
            yield _DONE  # pragma: no cover

        router = Router(await _pool("a", "b"), _RecordingStore())
        _mount(router, _race_handler({"a": preferred, "b": _silent(never)}))
        receipt = CallReceipt()
        loop = asyncio.get_running_loop()
        started = loop.time()
        async with aclosing(_stream(router, receipt, fastest_of=2)) as deltas:
            first = await asyncio.wait_for(anext(deltas), timeout=2.0)
        return first, receipt.llm_name, loop.time() - started

    first, named, elapsed = asyncio.run(run())
    assert (first, named) == ("a-first", "a")
    # The default window is a whole second: the preferred lane may not be made to wait it out.
    assert elapsed < 0.5  # noqa: PLR2004


def test_an_earlier_reserve_delta_is_buffered_until_the_preferred_lane_begins():
    """A reserve that opens first is consumed and held: the caller sees the pool's own
    first choice, and never a delta from both."""

    async def run():
        never = asyncio.Event()

        async def preferred():
            await asyncio.sleep(0.05)
            yield _delta("a-first")
            await never.wait()
            yield _DONE  # pragma: no cover

        async def reserve():
            yield _delta("b-first")
            await never.wait()
            yield _DONE  # pragma: no cover

        router = Router(await _pool("a", "b"), _RecordingStore())
        _mount(router, _race_handler({"a": preferred, "b": reserve}))
        receipt = CallReceipt()
        seen: list[str] = []
        async with aclosing(_stream(router, receipt, fastest_of=2)) as deltas:
            seen.append(await asyncio.wait_for(anext(deltas), timeout=2.0))
        return seen, receipt.llm_name

    seen, named = asyncio.run(run())
    assert (seen, named) == (["a-first"], "a")


def test_buffered_reserve_output_is_released_when_the_window_expires():
    """The preferred lane's privilege is bounded: what a reserve already produced becomes
    the stream the moment the window closes."""

    async def run():
        never = asyncio.Event()

        async def reserve():
            yield _delta("b-first")
            await never.wait()
            yield _DONE  # pragma: no cover

        router = Router(await _pool("a", "b"), _RecordingStore())
        _mount(router, _race_handler({"a": _silent(never), "b": reserve}))
        receipt = CallReceipt()
        loop = asyncio.get_running_loop()
        started = loop.time()
        async with aclosing(
            _stream(router, receipt, fastest_of=2, stream_selection_window=0.1),
        ) as deltas:
            first = await asyncio.wait_for(anext(deltas), timeout=2.0)
        return first, receipt.llm_name, loop.time() - started

    first, named, elapsed = asyncio.run(run())
    assert (first, named) == ("b-first", "b")
    assert elapsed >= 0.1  # noqa: PLR2004


def test_with_every_lane_silent_at_expiry_the_next_delta_selects_the_stream():
    async def run():
        never = asyncio.Event()

        async def late_reserve():
            await asyncio.sleep(0.1)
            yield _delta("b-first")
            await never.wait()
            yield _DONE  # pragma: no cover

        router = Router(await _pool("a", "b"), _RecordingStore())
        _mount(router, _race_handler({"a": _silent(never), "b": late_reserve}))
        receipt = CallReceipt()
        async with aclosing(
            _stream(router, receipt, fastest_of=2, stream_selection_window=0.02),
        ) as deltas:
            first = await asyncio.wait_for(anext(deltas), timeout=2.0)
        return first, receipt.llm_name

    assert asyncio.run(run()) == ("b-first", "b")


def test_a_zero_window_removes_the_ranked_preference():
    async def run():
        never = asyncio.Event()

        async def preferred():
            await asyncio.sleep(0.1)
            yield _delta("a-first")
            await never.wait()
            yield _DONE  # pragma: no cover

        async def reserve():
            yield _delta("b-first")
            await never.wait()
            yield _DONE  # pragma: no cover

        router = Router(await _pool("a", "b"), _RecordingStore())
        _mount(router, _race_handler({"a": preferred, "b": reserve}))
        receipt = CallReceipt()
        seen: list[str] = []
        async with aclosing(
            _stream(router, receipt, fastest_of=2, stream_selection_window=0),
        ) as deltas:
            seen.append(await asyncio.wait_for(anext(deltas), timeout=2.0))
        return seen, receipt.llm_name

    seen, named = asyncio.run(run())
    assert (seen, named) == (["b-first"], "b")


def test_a_complete_reserve_inside_the_window_is_replayed_without_replacement():
    """A whole answer beats the window: the caller has nothing to discard yet, so it is
    handed over as the ordinary stream rather than through the replacement path."""

    async def run():
        never = asyncio.Event()

        async def reserve():
            yield _sse("b-one", "b-two")

        store = _RecordingStore()
        router = Router(await _pool("a", "b"), store)
        _mount(router, _race_handler({"a": _silent(never), "b": reserve}))
        receipt = CallReceipt()
        deltas = [
            delta
            async for delta in _stream(router, receipt, fastest_of=2, stream_selection_window=5.0)
        ]
        return deltas, receipt, store

    deltas, receipt, store = asyncio.run(run())
    assert deltas == ["b-one", "b-two"]
    assert (receipt.llm_name, receipt.settled) == ("b", True)
    assert {row.llm_name: row.status for row in store.calls} == {
        "a": CallStatus.SUPERSEDED,
        "b": CallStatus.OK,
    }


def test_a_hidden_lane_completing_first_replaces_the_provisional_text():
    """The whole point: provisional deltas are discarded whole, never spliced, and the
    handle names the model whose complete answer won."""

    async def run():
        never = asyncio.Event()
        release = asyncio.Event()

        async def preferred():
            yield _delta("a-partial")
            await never.wait()
            yield _DONE  # pragma: no cover

        async def reserve():
            await release.wait()
            yield _sse("b-one", "b-two")

        store = _RecordingStore()
        router = Router(await _pool("a", "b"), store)
        _mount(router, _race_handler({"a": preferred, "b": reserve}))
        receipt = CallReceipt()
        seen: list[str] = []
        raised = None
        try:
            async with aclosing(_stream(router, receipt, fastest_of=2)) as deltas:
                async for delta in deltas:
                    seen.append(delta)
                    release.set()
        except StreamReplacementError as exc:
            raised = exc
        return seen, raised, receipt, store

    seen, raised, receipt, store = asyncio.run(run())
    assert seen == ["a-partial"]
    assert raised is not None
    assert raised.streamed_llm_name == "a"
    assert raised.replacement.text == "b-oneb-two"
    assert raised.replacement.llm_name == "b"
    assert (receipt.llm_name, receipt.call_id, receipt.settled) == (
        "b",
        raised.replacement.call_id,
        True,
    )
    assert {row.llm_name: row.status for row in store.calls} == {
        "a": CallStatus.SUPERSEDED,
        "b": CallStatus.OK,
    }


def test_a_provisional_lane_completing_first_supersedes_every_reserve():
    async def run():
        never = asyncio.Event()
        finish = asyncio.Event()

        async def preferred():
            yield _delta("a-one")
            await finish.wait()
            yield _delta("a-two") + _DONE

        store = _RecordingStore()
        router = Router(await _pool("a", "b"), store)
        _mount(router, _race_handler({"a": preferred, "b": _silent(never)}))
        receipt = CallReceipt()
        seen: list[str] = []
        async with aclosing(_stream(router, receipt, fastest_of=2)) as deltas:
            seen.append(await asyncio.wait_for(anext(deltas), timeout=2.0))
            finish.set()
            async for delta in deltas:
                seen.append(delta)
        return seen, receipt, store

    seen, receipt, store = asyncio.run(run())
    assert seen == ["a-one", "a-two"]
    assert (receipt.llm_name, receipt.settled) == ("a", True)
    assert {row.llm_name: row.status for row in store.calls} == {
        "a": CallStatus.OK,
        "b": CallStatus.SUPERSEDED,
    }


def test_a_preferred_failure_ends_its_selection_privilege_at_once():
    """A lane that will never begin holds nothing back, and the field it emptied is
    refilled from a model this call has not tried."""
    requested: list[str] = []

    async def run():
        never = asyncio.Event()

        async def refilled():
            yield _delta("c-first")
            await never.wait()
            yield _DONE  # pragma: no cover

        bodies = {"b": _silent(never), "c": refilled}

        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host or ""
            requested.append(host)
            if host == "a":
                return httpx.Response(503, text="down")
            return httpx.Response(
                200,
                content=bodies[host](),
                headers={"content-type": "text/event-stream"},
            )

        pool = await _pool("a", "b", "c")
        store = _RecordingStore()
        router = Router(pool, store)
        _mount(router, handler)
        receipt = CallReceipt()
        loop = asyncio.get_running_loop()
        started = loop.time()
        async with aclosing(
            _stream(
                router,
                receipt,
                fastest_of=2,
                parallel_recovery=False,
                stream_selection_window=5.0,
            ),
        ) as deltas:
            first = await asyncio.wait_for(anext(deltas), timeout=2.0)
        return first, receipt.llm_name, loop.time() - started, pool, store

    first, named, elapsed, pool, store = asyncio.run(run())
    assert (first, named) == ("c-first", "c")
    assert set(requested) == {"a", "b", "c"}
    # Nothing waited out the five-second window the dead lane no longer owns.
    assert elapsed < 2.0  # noqa: PLR2004
    rows = {row.llm_name: row for row in store.calls}
    assert rows["a"].status is CallStatus.UNAVAILABLE
    assert pool.state("a").phase is LifecyclePhase.COOLING


def test_a_hidden_real_failure_keeps_its_evidence_while_another_lane_answers():
    async def run():
        tried = asyncio.Event()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "b":
                tried.set()
                return httpx.Response(503, text="down")

            async def body():
                # The hidden lane must reach its provider before the answer lands, or it
                # is retired without ever being tried and there is no evidence to keep.
                await tried.wait()
                yield _sse("a-answer")

            return httpx.Response(
                200,
                content=body(),
                headers={"content-type": "text/event-stream"},
            )

        optimizer = Optimizer()
        pool = await _pool("a", "b", optimizer=optimizer)
        store = _RecordingStore()
        router = Router(pool, store, optimizer=optimizer, learner=Learner(optimizer, store, pool))
        _mount(router, handler)
        deltas = await asyncio.wait_for(
            _drain(router, fastest_of=2, parallel_recovery=False),
            timeout=2.0,
        )
        return deltas, pool, optimizer, store

    deltas, pool, optimizer, store = asyncio.run(run())
    assert deltas == ["a-answer"]
    rows = {row.llm_name: row for row in store.calls}
    assert rows["b"].status is CallStatus.UNAVAILABLE
    assert rows["b"].cooldown_until is not None
    assert pool.state("b").phase is LifecyclePhase.COOLING
    assert optimizer.rl_fail_count("b") == 1
    assert rows["a"].status is CallStatus.OK


def test_a_provisional_real_failure_is_rescued_by_a_later_complete_reserve():
    """The lane the caller was reading died mid-answer, and a reserve was still viable:
    that complete answer replaces the partial text instead of ending the call."""

    async def run():
        release = asyncio.Event()

        async def preferred():
            yield _delta("a-partial")
            await release.wait()
            raise httpx.ReadError("the provisional lane died")

        async def reserve():
            await release.wait()
            yield _sse("b-answer")

        optimizer = Optimizer()
        pool = await _pool("a", "b", optimizer=optimizer)
        store = _RecordingStore()
        router = Router(pool, store, optimizer=optimizer, learner=Learner(optimizer, store, pool))
        _mount(router, _race_handler({"a": preferred, "b": reserve}))
        receipt = CallReceipt()
        seen: list[str] = []
        raised = None
        try:
            async with aclosing(_stream(router, receipt, fastest_of=2)) as deltas:
                async for delta in deltas:
                    seen.append(delta)
                    release.set()
        except StreamReplacementError as exc:
            raised = exc
        return seen, raised, receipt, pool, store

    seen, raised, receipt, pool, store = asyncio.run(run())
    assert seen == ["a-partial"]
    assert raised is not None
    assert (raised.streamed_llm_name, raised.replacement.text) == ("a", "b-answer")
    assert receipt.llm_name == "b"
    rows = {row.llm_name: row for row in store.calls}
    assert rows["a"].status is CallStatus.ERROR
    assert pool.state("a").phase is LifecyclePhase.COOLING
    assert rows["b"].status is CallStatus.OK


def test_beyond_two_lanes_only_the_preferred_one_uses_pool_order():
    """The bounded preference belongs to the pool's first choice alone; reserves compete
    on the clock, so the earlier delta wins even from further down the order."""

    async def run():
        never = asyncio.Event()

        async def early():
            yield _delta("c-first")
            await never.wait()
            yield _DONE  # pragma: no cover

        async def later():
            await asyncio.sleep(0.05)
            yield _delta("b-first")
            await never.wait()
            yield _DONE  # pragma: no cover

        router = Router(await _pool("a", "b", "c"), _RecordingStore())
        _mount(router, _race_handler({"a": _silent(never), "b": later, "c": early}))
        receipt = CallReceipt()
        async with aclosing(
            _stream(
                router,
                receipt,
                fastest_of=3,
                parallel_recovery=False,
                stream_selection_window=0.2,
            ),
        ) as deltas:
            first = await asyncio.wait_for(anext(deltas), timeout=2.0)
        return first, receipt.llm_name

    assert asyncio.run(run()) == ("c-first", "c")


def test_racing_a_stream_with_recovery_adds_no_third_lane():
    requested: list[str] = []

    async def run():
        never = asyncio.Event()

        async def answering():
            yield _sse("b-answer")

        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host or ""
            requested.append(host)
            body = _silent(never)() if host == "a" else answering()
            return httpx.Response(
                200,
                content=body,
                headers={"content-type": "text/event-stream"},
            )

        pool = await _pool("a", "b", "c")
        await _expire_cooldown(pool, "a")
        router = Router(pool, _RecordingStore())
        _mount(router, handler)
        return await asyncio.wait_for(
            _drain(router, fastest_of=2, parallel_recovery=True),
            timeout=2.0,
        )

    assert asyncio.run(run()) == ["b-answer"]
    assert set(requested) == {"a", "b"}


def test_a_one_model_pool_runs_one_lane_under_an_explicit_race():
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.host or "")
        return httpx.Response(
            200,
            content=_sse("only-one", "and-the-rest"),
            headers={"content-type": "text/event-stream"},
        )

    async def run():
        store = _RecordingStore()
        router = Router(await _pool("a"), store)
        _mount(router, handler)
        receipt = CallReceipt()
        deltas = [delta async for delta in _stream(router, receipt, fastest_of=2)]
        return deltas, receipt, store

    deltas, receipt, store = asyncio.run(run())
    assert deltas == ["only-one", "and-the-rest"]
    assert requested == ["a"]
    assert (receipt.llm_name, receipt.settled) == ("a", True)
    assert [row.status for row in store.calls] == [CallStatus.OK]


def test_abandoning_a_raced_stream_settles_the_exposed_lane_as_answered():
    """Today's host-abandonment contract: the lane the host was reading is the call it
    chose to stop, so it is answered and rateable, and the hidden lanes are neutral."""

    async def run():
        never = asyncio.Event()

        async def preferred():
            yield _delta("a-first")
            await never.wait()
            yield _DONE  # pragma: no cover

        pool = await _pool("a", "b")
        store = _RecordingStore()
        router = Router(pool, store)
        _mount(router, _race_handler({"a": preferred, "b": _silent(never)}))
        receipt = CallReceipt()
        async with aclosing(_stream(router, receipt, fastest_of=2)) as deltas:
            seen = [await asyncio.wait_for(anext(deltas), timeout=2.0)]
        return seen, receipt, pool, store

    seen, receipt, pool, store = asyncio.run(run())
    assert seen == ["a-first"]
    assert (receipt.llm_name, receipt.settled) == ("a", True)
    assert {row.llm_name: row.status for row in store.calls} == {
        "a": CallStatus.OK,
        "b": CallStatus.SUPERSEDED,
    }
    assert [pool._slots[name].in_flight for name in ("a", "b")] == [0, 0]  # noqa: SLF001


def test_a_raced_stream_cancelled_inside_the_window_leaves_nothing_behind():
    async def run():
        never = asyncio.Event()
        opened = asyncio.Event()
        starts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal starts
            starts += 1
            if starts == 2:  # noqa: PLR2004
                opened.set()
            return httpx.Response(
                200,
                content=_silent(never)(),
                headers={"content-type": "text/event-stream"},
            )

        pool = await _pool("a", "b")
        store = _RecordingStore()
        router = Router(pool, store)
        _mount(router, handler)
        receipt = CallReceipt()
        async with aclosing(
            _stream(router, receipt, fastest_of=2, stream_selection_window=5.0),
        ) as deltas:
            pulling = asyncio.create_task(anext(deltas))
            await asyncio.wait_for(opened.wait(), timeout=2.0)
            pulling.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pulling
        left = asyncio.all_tasks() - {asyncio.current_task()}
        return receipt, pool, store, left

    receipt, pool, store, left = asyncio.run(run())
    assert receipt.llm_name is None
    assert {row.llm_name: row.status for row in store.calls} == {
        "a": CallStatus.SUPERSEDED,
        "b": CallStatus.SUPERSEDED,
    }
    assert [pool._slots[name].in_flight for name in ("a", "b")] == [0, 0]  # noqa: SLF001
    assert left == set()


def test_the_winner_is_the_first_provider_completion_not_the_first_row_written():
    """A store slow on one lane may not reorder the race: what decides is the instant each
    provider finished, taken before the row is handed to the backend."""

    async def _unused():
        yield ""  # pragma: no cover - nothing drives these lanes

    lanes = [
        _StreamLane(config=_cfg(name), outcome=_Outcome(), produced=_unused())
        for name in ("a", "b")
    ]
    race = _StreamRace(lanes=lanes, width=2)
    assert race.winner() is None
    # The reserve's provider finished first; the preferred lane's row would land first.
    lanes[1].outcome.completed_at = 1.0
    lanes[0].outcome.completed_at = 2.0
    assert race.winner() is lanes[1]


def test_a_bug_in_every_lane_reaches_the_caller_instead_of_being_retried():
    """A bug is not a failure this call may fail over from: nothing classified it, so the
    same candidates stay eligible and reopening them would only repeat it forever."""
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.host or "")
        raise ValueError("adapter bug")

    async def run():
        router = Router(await _pool("a", "b"), _RecordingStore())
        _mount(router, handler)
        with pytest.raises(ValueError, match="adapter bug"):
            await asyncio.wait_for(_drain(router, fastest_of=2), timeout=1.0)

    asyncio.run(run())
    assert sorted(requested) == ["a", "b"]


def test_a_losing_lane_leaves_its_provider_before_the_winners_row_lands():
    """A complete answer retires its siblings the instant it exists, not once its row has
    landed: a slow store may be paid for in neither the caller's latency nor quota."""

    async def run():
        held = asyncio.Event()
        both_open = asyncio.Event()
        loser_cancelled = asyncio.Event()
        never = asyncio.Event()

        class _HeldAnswerRow(_RecordingStore):
            async def record(self, call):
                if call.status is CallStatus.OK:
                    await held.wait()
                await super().record(call)

        async def winner_body():
            await both_open.wait()
            yield _sse("a-answer")

        async def loser_body():
            try:
                yield _delta("b-partial")
                await never.wait()
            finally:
                loser_cancelled.set()

        store = _HeldAnswerRow()
        router = Router(await _pool("a", "b"), store)
        _mount(router, _paired_handler({"a": winner_body, "b": loser_body}, both_open))
        seen: list[str] = []
        async with aclosing(_stream(router, fastest_of=2, parallel_recovery=False)) as deltas:
            seen.append(await asyncio.wait_for(anext(deltas), timeout=1.0))
            await asyncio.wait_for(loser_cancelled.wait(), timeout=1.0)
            answered_by_then = [row for row in store.calls if row.status is CallStatus.OK]
            held.set()
            async for delta in deltas:
                seen.append(delta)
        return seen, answered_by_then, store.calls

    seen, answered_by_then, calls = asyncio.run(run())
    assert seen == ["a-answer"]
    # The loser was off its provider and the delta was with the caller while the winner's
    # own row was still in the store.
    assert answered_by_then == []
    assert {row.llm_name: row.status for row in calls} == {
        "a": CallStatus.OK,
        "b": CallStatus.SUPERSEDED,
    }


def test_a_lane_cancelled_before_its_attempt_began_gives_its_slot_back():
    """A task cancelled before its first step runs none of the attempt's own code, so it
    journals nothing and the slot acquired for it can only go back from the driver."""

    async def run():
        pool = await _pool("a")
        taken = await pool.acquire(None, payable=frozenset({"K"}))
        store = _RecordingStore()
        router = Router(pool, store)

        async def never_runs():
            yield ""  # pragma: no cover - cancelled before its first step

        lane = _StreamLane(config=taken, outcome=_Outcome(), produced=never_runs())
        lane.task = asyncio.create_task(Router._drain_lane(lane, asyncio.Event()))  # noqa: SLF001
        lane.task.cancel()
        await asyncio.wait_for(router._stop(lane), timeout=1.0)  # noqa: SLF001
        return pool, store

    pool, store = asyncio.run(run())
    assert store.calls == []
    assert pool._slots["a"].in_flight == 0  # noqa: SLF001


def test_a_losing_lane_is_retired_while_the_reader_holds_a_delta():
    """Retirement is a lane reporting, never the caller reading: the driver is suspended
    on a yield between deltas, and a paused reader may not keep a loser on its provider."""

    async def run():
        held = asyncio.Event()
        both_open = asyncio.Event()
        finish = asyncio.Event()
        never = asyncio.Event()
        loser_cancelled = asyncio.Event()

        class _HeldAnswerRow(_RecordingStore):
            async def record(self, call):
                if call.status is CallStatus.OK:
                    await held.wait()
                await super().record(call)

        async def preferred():
            await both_open.wait()
            yield _delta("a-first")
            await finish.wait()
            yield _sse("a-rest")

        async def reserve():
            try:
                yield _delta("b-partial")
                await never.wait()
                yield _DONE  # pragma: no cover - the lane is cancelled first
            finally:
                loser_cancelled.set()

        store = _HeldAnswerRow()
        router = Router(await _pool("a", "b"), store)
        _mount(router, _paired_handler({"a": preferred, "b": reserve}, both_open))
        seen: list[str] = []
        async with aclosing(_stream(router, fastest_of=2, parallel_recovery=False)) as deltas:
            seen.append(await asyncio.wait_for(anext(deltas), timeout=2.0))
            # Nothing pulls the driver on from here: the reader is holding that delta.
            finish.set()
            await asyncio.wait_for(loser_cancelled.wait(), timeout=2.0)
            answered_by_then = [row for row in store.calls if row.status is CallStatus.OK]
            held.set()
            async for delta in deltas:
                seen.append(delta)
        return seen, answered_by_then, store.calls

    seen, answered_by_then, calls = asyncio.run(run())
    assert seen == ["a-first", "a-rest"]
    assert answered_by_then == []
    assert {row.llm_name: row.status for row in calls} == {
        "a": CallStatus.OK,
        "b": CallStatus.SUPERSEDED,
    }


def test_the_selection_window_is_judged_by_when_the_delta_landed():
    """The coordinator may reach the decision a scheduling tick late, and the first
    choice neither loses a claim it made inside its window nor gains one it made after."""

    async def _unused():
        yield ""  # pragma: no cover - nothing drives these lanes

    def _race(preferred_at: float, reserve_at: float, closed: float) -> _StreamRace:
        lanes = [
            _StreamLane(config=_cfg(name), outcome=_Outcome(), produced=_unused())
            for name in ("a", "b")
        ]
        lanes[0].first_delta_at, lanes[1].first_delta_at = preferred_at, reserve_at
        return _StreamRace(lanes=lanes, width=2, deadline=closed)

    closed = time.monotonic() - 1.0
    inside = _race(closed - 0.1, closed - 0.5, closed)
    assert inside.select() is inside.lanes[0]
    late = _race(closed + 0.1, closed - 0.5, closed)
    assert late.select() is late.lanes[1]


@pytest.mark.parametrize(
    "window",
    [True, -1, -0.5, float("inf"), float("nan"), "1", None],
)
def test_invalid_stream_selection_window_opens_no_provider_request(window):
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.host or "")  # pragma: no cover
        return httpx.Response(200, content=_sse("x"), headers={})  # pragma: no cover

    async def run():
        router = Router(await _pool("a", "b"), _RecordingStore())
        _mount(router, handler)
        with pytest.raises(ValueError, match="stream_selection_window"):
            await _drain(router, fastest_of=2, stream_selection_window=window)

    asyncio.run(run())
    assert requested == []


def test_the_selection_window_is_on_the_async_routed_stream_only():
    for method in (AsyncBroker.stream, AsyncLLMs.stream):
        parameter = inspect.signature(method).parameters["stream_selection_window"]
        assert parameter.default == 1.0
    absent = (
        AsyncBroker.ask,
        AsyncBroker.chat,
        AsyncLLMs.ask,
        AsyncLLMs.chat,
        Broker.ask,
        Broker.chat,
        LLMs.ask,
        LLMs.chat,
        AsyncBroker.direct,
    )
    for method in absent:
        assert "stream_selection_window" not in inspect.signature(method).parameters

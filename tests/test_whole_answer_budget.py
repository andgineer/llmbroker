"""``wait`` bounds the whole answer, in provider time: the clock is disarmed across
every yield and pushed on by what the consumer held, so reading slowly can never spend
it. Past the first delta an exhausted budget ends the call — the model answered and did
nothing wrong, so it is journaled as a budget it did not finish within, never cooled, and
nothing is retried once output has reached the caller.
"""

import asyncio

import httpx
import pytest

from llmbroker.broker.learning import Learner
from llmbroker.broker.pool import LLMPool
from llmbroker.broker.result import CallReceipt
from llmbroker.broker.router import Router
from llmbroker.exceptions import (
    LLMTimeoutError,
    NoLLMAvailableError,
    StreamInterruptedError,
)
from llmbroker.models import CallStatus, LifecyclePhase, LLMConfig
from llmbroker.optimizer import Optimizer

from support import make_ring

_SSE = {"content-type": "text/event-stream"}
_DONE = b"data: [DONE]\n\n"


class _RecordingStore:
    def __init__(self) -> None:
        self.rows: list = []

    async def record(self, call) -> None:
        self.rows.append(call)

    async def record_quality(self, call_id, score, *, scope=None):
        pass

    async def calls(self, *, limit: int, **_kw) -> list:
        return self.rows[::-1][:limit]


def _cfg(name: str) -> LLMConfig:
    return LLMConfig(name=name, base_url=f"https://{name}/v1", model="m", api_key_ref="K")


async def _router(*names: str) -> tuple[Router, LLMPool, _RecordingStore]:
    """Pool in curated order: the first name given is the preferred model."""
    pool = LLMPool()
    for order, name in enumerate(names):
        await pool.add(_cfg(name), order)
    store = _RecordingStore()
    return Router(pool, store, learner=Learner(Optimizer(), store, pool)), pool, store


def _mount(router: Router, handler) -> None:
    router._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=30.0)  # noqa: SLF001


def _delta(text: str) -> bytes:
    return b'data: {"choices": [{"delta": {"content": "%s"}}]}\n\n' % text.encode()


def _responder(body):
    return lambda _r: httpx.Response(200, content=body(), headers=_SSE)


def _stream(router: Router, receipt: CallReceipt | None = None, **kwargs):
    return router.stream(
        make_ring(),
        [{"role": "user", "content": "hi"}],
        receipt if receipt is not None else CallReceipt(),
        **kwargs,
    )


async def _drain(router: Router, **kwargs) -> list[str]:
    return [d async for d in _stream(router, **kwargs)]


async def _dribbles():
    """Opens at once and then keeps going far past any budget under test."""
    yield _delta("one")
    for _ in range(50):
        await asyncio.sleep(0.05)
        yield _delta("more")
    yield _DONE


async def _prompt_body():
    yield _delta("one")
    yield _delta("two")
    yield _delta("three")
    yield _DONE


# ── the budget covers the answer, not only its opening ───────────────────────


def test_a_budget_bounds_the_whole_answer_however_it_is_chunked():
    """The point: a model that opens at once and then takes its time is inside no
    budget just because its first delta was prompt."""

    async def run():
        router, _, _ = await _router("a")
        _mount(router, _responder(_dribbles))
        with pytest.raises(LLMTimeoutError):
            await _drain(router, wait=0.3)

    asyncio.run(run())


def test_the_deltas_already_yielded_stand():
    async def run():
        router, _, _ = await _router("a")
        _mount(router, _responder(_dribbles))
        seen: list[str] = []
        with pytest.raises(LLMTimeoutError):
            async for delta in _stream(router, wait=0.3):
                seen.append(delta)
        return seen

    assert asyncio.run(run())[0] == "one"


def test_a_slow_consumer_never_spends_the_budget():
    """The load-bearing one: the clock runs only while the library waits on the
    provider, so a consumer that dawdles between deltas can never trip it."""

    async def run():
        router, _, _ = await _router("a")
        _mount(router, _responder(_prompt_body))
        seen: list[str] = []
        async for delta in _stream(router, wait=0.2):
            seen.append(delta)
            await asyncio.sleep(0.3)  # far longer than the whole budget
        return seen

    assert asyncio.run(run()) == ["one", "two", "three"]


def test_a_budget_still_bounds_the_wait_for_the_first_delta():
    """Unchanged: before any delta the same budget ends the call, blaming the caller's
    clock rather than the model."""

    async def slow_to_open():
        await asyncio.sleep(5)
        yield _delta("one")
        yield _DONE

    async def run():
        router, pool, _ = await _router("a")
        _mount(router, _responder(slow_to_open))
        async with asyncio.timeout(5):
            with pytest.raises(NoLLMAvailableError):
                await _drain(router, wait=0.3)
        return pool.state("a").phase, pool.state("a").fail_count

    assert asyncio.run(run()) == (LifecyclePhase.AVAILABLE, 0)


def test_no_budget_leaves_streaming_unbounded():
    """Unset is off: the library bounds nothing it was not asked to."""

    async def run():
        router, pool, store = await _router("a")
        _mount(router, _responder(_prompt_body))
        async with asyncio.timeout(10):
            deltas = await _drain(router)
        return deltas, [row.status for row in store.rows], pool.state("a").phase

    assert asyncio.run(run()) == (
        ["one", "two", "three"],
        [CallStatus.OK],
        LifecyclePhase.AVAILABLE,
    )


# ── what an exhausted budget is, and what it is not ──────────────────────────


def test_an_exhausted_budget_does_not_cool_the_model():
    """The model answered and did nothing wrong; cooling it would take it from every
    other caller over one caller's clock."""

    async def run():
        router, pool, store = await _router("a")
        _mount(router, _responder(_dribbles))
        with pytest.raises(LLMTimeoutError):
            await _drain(router, wait=0.3)
        return pool, store.rows

    pool, rows = asyncio.run(run())
    assert pool.state("a").phase is LifecyclePhase.AVAILABLE
    assert pool.state("a").fail_count == 0
    assert [row.cooldown_until for row in rows] == [None]


def test_an_exhausted_budget_journals_the_time_it_did_not_finish_within():
    async def run():
        router, _, store = await _router("a")
        _mount(router, _responder(_dribbles))
        with pytest.raises(LLMTimeoutError):
            await _drain(router, wait=0.3)
        return store.rows

    (row,) = asyncio.run(run())
    assert row.status is CallStatus.ERROR
    assert row.budget_ms >= 300  # noqa: PLR2004


def test_an_exhausted_budget_is_not_a_stream_death():
    """ "The stream died" and "my own budget ran out" are different states for a host."""

    async def run():
        router, _, _ = await _router("a")
        _mount(router, _responder(_dribbles))
        with pytest.raises(LLMTimeoutError) as excinfo:
            await _drain(router, wait=0.3)
        return excinfo.value

    assert not isinstance(asyncio.run(run()), StreamInterruptedError)


def test_the_next_caller_is_handed_a_sibling():
    """The miss is evidence like any other: an equally tight caller goes elsewhere."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = _dribbles if request.url.host == "a" else _prompt_body
        return httpx.Response(200, content=body(), headers=_SSE)

    async def run():
        router, _, _ = await _router("a", "b")
        _mount(router, handler)
        with pytest.raises(LLMTimeoutError):
            await _drain(router, wait=0.3)

        receipt = CallReceipt()
        async with asyncio.timeout(5):
            _ = [d async for d in _stream(router, receipt, wait=0.5)]
        return receipt.llm_name

    assert asyncio.run(run()) == "b"

"""`wait` bounds the in-flight attempt, not only slot acquisition — and a model that
produces nothing for the whole of it is cooled like any other failed attempt, while the
caller still gets its own expired budget back.
"""

import asyncio
import time
from unittest.mock import patch

import httpx
import pytest

from llmbroker import chat
from llmbroker.broker.learning import Learner
from llmbroker.broker.pool import LLMPool
from llmbroker.broker.router import Router
from llmbroker.exceptions import NoLLMAvailableError, ProviderError
from llmbroker.models import CallStatus, LifecyclePhase, LLMConfig
from llmbroker.optimizer import Optimizer

from support import make_ring

_PATCH = "llmbroker.broker.router.call_provider"


class _RecordingStore:
    def __init__(self) -> None:
        self.calls: list = []

    async def record(self, call):
        self.calls.append(call)

    async def record_quality(self, call_id, score, *, scope=None):
        pass


def _cfg(name="p1"):
    return LLMConfig(name=name, base_url="https://x/v1", model="m", api_key_ref="K")


async def _pool(*cfgs) -> LLMPool:
    pool = LLMPool()
    for order, cfg in enumerate(cfgs):
        await pool.add(cfg, order)
    return pool


def _fake_provider(captured: list, *, raises=None, results=None):
    """Record the per-attempt timeout the router hands down; then answer or fail."""
    outcomes = list(results or [])

    async def fake(config, api_key, messages, tools, *, client=None, timeout=None):  # noqa: ARG001
        captured.append(timeout)
        if outcomes:
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        if raises is not None:
            raise raises
        return "ok", None, None

    return fake


# ---------------------------------------------------------------------------
# The timeout handed to the provider call
# ---------------------------------------------------------------------------


def test_attempt_timeout_is_the_remaining_budget_when_it_is_the_smaller_bound():
    async def run():
        router = Router(await _pool(_cfg()), _RecordingStore())
        captured: list = []
        with patch(_PATCH, new=_fake_provider(captured)):
            await router.chat(make_ring(), [{"role": "user", "content": "hi"}], wait=10.0)
        assert 9.0 < captured[0] <= 10.0

    asyncio.run(run())


def test_attempt_timeout_is_the_global_ceiling_when_it_is_the_smaller_bound():
    async def run():
        router = Router(await _pool(_cfg()), _RecordingStore())
        captured: list = []
        with (
            patch.object(chat, "HTTP_TIMEOUT", 2.0),
            patch(_PATCH, new=_fake_provider(captured)),
        ):
            await router.chat(make_ring(), [{"role": "user", "content": "hi"}], wait=30.0)
        assert captured == [2.0]

    asyncio.run(run())


def test_wait_none_attempt_runs_on_the_global_ceiling():
    async def run():
        router = Router(await _pool(_cfg()), _RecordingStore())
        captured: list = []
        with patch(_PATCH, new=_fake_provider(captured)):
            await router.chat(make_ring(), [{"role": "user", "content": "hi"}])
        assert captured == [chat.HTTP_TIMEOUT]

    asyncio.run(run())


def test_wait_zero_means_do_not_queue_not_answer_instantly():
    """wait=0 bounds slot acquisition only; the attempt keeps the global ceiling,
    or no free model could ever answer a non-blocking call."""

    async def run():
        router = Router(await _pool(_cfg()), _RecordingStore())
        captured: list = []
        with patch(_PATCH, new=_fake_provider(captured)):
            result = await router.chat(make_ring(), [{"role": "user", "content": "hi"}], wait=0)
        assert result.text == "ok"
        assert captured == [chat.HTTP_TIMEOUT]

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Silence for the whole budget cools the model
# ---------------------------------------------------------------------------


def test_silence_for_the_whole_budget_cools_the_model():
    """To the caller an endpoint that produces nothing for the whole budget is a dead
    one: it is cooled and counted, while the caller still gets its own expired wait."""

    async def run():
        pool = await _pool(_cfg())
        store = _RecordingStore()
        router = Router(pool, store)
        with patch(_PATCH, new=_fake_provider([], raises=httpx.ReadTimeout("hung"))):
            with pytest.raises(NoLLMAvailableError) as exc_info:
                await router.chat(make_ring(), [{"role": "user", "content": "hi"}], wait=5.0)

        assert exc_info.value.reason == "timeout"
        # Nobody is left to serve now, so the caller is told when the pool is back.
        assert exc_info.value.retry_at == pool.state("p1").cooldown_until
        assert pool.state("p1").phase is LifecyclePhase.COOLING
        assert pool.state("p1").fail_count == 1
        assert pool._slots["p1"].in_flight == 0
        (row,) = store.calls
        assert row.status is CallStatus.ERROR  # unclassified by design
        assert row.cooldown_until is not None
        assert row.budget_ms is not None  # availability did not erase the ordering evidence

    asyncio.run(run())


def test_a_sibling_that_can_serve_now_leaves_retry_at_unset():
    """`retry_at` answers "when is the pool back", not "when is that model back": with
    another model free right now there is no better moment than this one."""

    async def run():
        pool = await _pool(_cfg("a"), _cfg("b"))
        router = Router(pool, _RecordingStore())
        with patch(_PATCH, new=_fake_provider([], raises=httpx.ReadTimeout("hung"))):
            with pytest.raises(NoLLMAvailableError) as exc_info:
                await router.chat(make_ring(), [{"role": "user", "content": "hi"}], wait=5.0)

        assert exc_info.value.reason == "timeout"
        assert exc_info.value.retry_at is None
        assert pool.state("a").phase is LifecyclePhase.COOLING

    asyncio.run(run())


def test_silence_costs_the_ordinary_base_and_escalates_only_on_the_streak():
    """No duration of its own: the first silence costs exactly what a transport failure
    costs, and a repeat climbs the same ladder every other cooled failure climbs."""

    async def run():
        pool = await _pool(_cfg("silent"), _cfg("broken"))
        store = _RecordingStore()
        optimizer = Optimizer()
        router = Router(
            pool,
            store,
            optimizer=optimizer,
            learner=Learner(optimizer, store, pool),
        )
        with patch(_PATCH, new=_fake_provider([], raises=httpx.ReadTimeout("hung"))):
            with pytest.raises(NoLLMAvailableError):  # silence inside the budget
                await router.chat(make_ring(), [{"role": "user", "content": "hi"}], wait=5.0)
            with pytest.raises(NoLLMAvailableError):  # a transport failure, no budget
                await router.chat(make_ring(), [{"role": "user", "content": "hi"}], wait=0)
            pool.clear_cooling("silent")
            with pytest.raises(NoLLMAvailableError):  # silent again, one streak deeper
                await router.chat(make_ring(), [{"role": "user", "content": "hi"}], wait=5.0)
        return store.calls

    rows = asyncio.run(run())
    assert [c.llm_name for c in rows] == ["silent", "broken", "silent"]
    first_silence, transport, second_silence = (
        (c.cooldown_until - c.ts).total_seconds() for c in rows
    )
    assert first_silence == pytest.approx(transport, abs=0.5)
    assert second_silence == pytest.approx(2 * first_silence, abs=0.5)


def test_budget_expiry_does_not_try_the_next_model_either():
    """The caller's clock ran out — there is no time left for a sibling."""

    async def run():
        pool = await _pool(_cfg("a"), _cfg("b"))
        router = Router(pool, _RecordingStore())
        captured: list = []
        with patch(_PATCH, new=_fake_provider(captured, raises=httpx.ReadTimeout("hung"))):
            with pytest.raises(NoLLMAvailableError):
                await router.chat(make_ring(), [{"role": "user", "content": "hi"}], wait=5.0)
        assert len(captured) == 1
        assert pool.state("b").phase is LifecyclePhase.AVAILABLE

    asyncio.run(run())


def test_a_client_error_already_seen_outranks_the_expired_budget():
    """A 400 says the request itself is wrong; "the clock ran out" does not. When
    both happened in one call the caller gets the one it can act on — the same
    preference the exhausted-candidates path already applies."""

    async def run():
        pool = await _pool(_cfg("a"), _cfg("b"))
        router = Router(pool, _RecordingStore())
        client_error = httpx.HTTPStatusError(
            "bad request",
            request=httpx.Request("POST", "https://x/v1/chat/completions"),
            response=httpx.Response(400, text="messages[0].role is invalid"),
        )
        provider = _fake_provider([], results=[client_error, httpx.ReadTimeout("hung")])
        with patch(_PATCH, new=provider):
            with pytest.raises(ProviderError) as exc_info:
                await router.chat(make_ring(), [{"role": "user", "content": "hi"}], wait=5.0)
        assert exc_info.value.status == 400
        assert exc_info.value.detail == "messages[0].role is invalid"

    asyncio.run(run())


def test_ceiling_bound_timeout_cools_the_model_and_fails_over():
    async def run():
        pool = await _pool(_cfg("a"), _cfg("b"))
        store = _RecordingStore()
        router = Router(pool, store)
        captured: list = []
        provider = _fake_provider(
            captured,
            results=[httpx.ReadTimeout("too slow"), ("ok", None, None)],
        )
        with patch.object(chat, "HTTP_TIMEOUT", 0.5), patch(_PATCH, new=provider):
            result = await router.chat(make_ring(), [{"role": "user", "content": "hi"}])

        assert result.text == "ok"
        assert captured == [0.5, 0.5]
        assert pool.state("a").phase is LifecyclePhase.COOLING
        row_a = next(c for c in store.calls if c.llm_name == "a")
        assert row_a.cooldown_until is not None

    asyncio.run(run())


def test_a_hung_provider_cannot_outlive_the_budget():
    """The bound is wall-clock, not httpx's per-operation timeout: a provider that
    never answers is abandoned when the budget runs out, not three timeouts later."""

    async def run():
        pool = await _pool(_cfg())
        router = Router(pool, _RecordingStore())

        async def hang(*args, **kwargs):
            await asyncio.sleep(30)

        started = time.monotonic()
        with patch(_PATCH, new=hang):
            with pytest.raises(NoLLMAvailableError) as exc_info:
                await router.chat(make_ring(), [{"role": "user", "content": "hi"}], wait=0.2)
        assert exc_info.value.reason == "timeout"
        assert time.monotonic() - started < 5.0
        assert pool._slots["p1"].in_flight == 0
        assert pool.state("p1").phase is LifecyclePhase.COOLING

    asyncio.run(run())


def test_a_hung_provider_cools_down_at_the_global_ceiling():
    """The same hang without a `wait` is the model's own fault: cool it, fail over."""

    async def run():
        pool = await _pool(_cfg("a"), _cfg("b"))
        router = Router(pool, _RecordingStore())
        calls = 0

        async def hang_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                await asyncio.sleep(30)
            return "ok", None, None

        with patch.object(chat, "HTTP_TIMEOUT", 0.2), patch(_PATCH, new=hang_once):
            result = await router.chat(make_ring(), [{"role": "user", "content": "hi"}])

        assert result.llm_name == "b"
        assert pool.state("a").phase is LifecyclePhase.COOLING
        # The ceiling firing is the model's fault and cools it; the budget-expiry
        # bound is the other mechanism, and the two must never stack on one failure.
        assert "a" not in pool._budget_bounds

    asyncio.run(run())


def test_exhausted_budget_short_circuits_the_next_attempt():
    """Once the budget is gone the router does not open another HTTP request.

    A negative ``wait`` is the honest way to express that: legal, and meaning the
    budget was spent before the call started.
    """

    async def run():
        pool = await _pool(_cfg())
        store = _RecordingStore()
        router = Router(pool, store)
        captured: list = []
        with patch(_PATCH, new=_fake_provider(captured)):
            with pytest.raises(NoLLMAvailableError) as exc_info:
                await router.chat(make_ring(), [{"role": "user", "content": "hi"}], wait=-1.0)
        assert exc_info.value.reason == "timeout"
        assert captured == []
        assert pool._slots["p1"].in_flight == 0
        assert store.calls[0].cooldown_until is None

    asyncio.run(run())

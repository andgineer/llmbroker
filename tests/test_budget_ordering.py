"""A budget expiry teaches ordering, not availability: the model that ate one
caller's whole budget stops being the first choice for equally tight ones, without
being cooled, penalised, or excluded.
"""

import asyncio
import time
from unittest.mock import patch

import pytest

from llmbroker import chat
from llmbroker.broker import pool as pool_module
from llmbroker.broker.pool import LLMPool
from llmbroker.broker.router import Router
from llmbroker.exceptions import NoLLMAvailableError
from llmbroker.models import LifecyclePhase, LLMConfig

_PATCH = "llmbroker.broker.router.call_provider"
_HANG_SEC = 30


class _RecordingStore:
    def __init__(self) -> None:
        self.calls: list = []

    async def record(self, call):
        self.calls.append(call)

    async def record_quality(self, llm_name, operation, score, *, call_id=None):
        pass


def _cfg(name: str) -> LLMConfig:
    return LLMConfig(name=name, base_url=f"https://{name}/v1", model="m", api_key_ref="K")


async def _router(*names: str) -> tuple[Router, LLMPool, _RecordingStore]:
    """Pool in curated order: the first name given is the preferred model."""
    pool = LLMPool()
    for order, name in enumerate(names):
        await pool.add(_cfg(name), "secret", order)
    store = _RecordingStore()
    router = Router(pool, store, scope=None)
    # The router builds its httpx client on the first attempt, inside the caller's
    # budget: an SSL context on a cold runner costs more than the 0.2s waits below,
    # which would expire the budget before any provider is reached — a different path
    # (and no bound recorded) than what these tests are about.
    router._http = chat.make_client()
    return router, pool, store


def _provider(hangs: set[str]):
    """Answer at once, unless this model is in ``hangs`` — then never."""

    async def fake(config, api_key, messages, tools, *, client=None, timeout=None):  # noqa: ARG001
        if config.name in hangs:
            await asyncio.sleep(_HANG_SEC)
        return f"{config.name} answered", None, None

    return fake


async def _ask(router: Router, **kwargs):
    return await router.chat([{"role": "user", "content": "hi"}], **kwargs)


def test_the_next_caller_does_not_walk_into_the_same_hang():
    """The whole point: caller one pays for discovering the hang, caller two does not."""

    async def run():
        router, pool, store = await _router("a", "b")
        hangs = {"a"}
        with patch(_PATCH, new=_provider(hangs)):
            with pytest.raises(NoLLMAvailableError):
                await _ask(router, wait=0.2)

            started = time.monotonic()
            result = await _ask(router, wait=0.2)

        assert result.llm_name == "b"
        assert time.monotonic() - started < 0.2  # not queued behind the hang again
        # nothing was held against `a`: no cooldown, no streak, still selectable
        assert pool.state("a").phase is LifecyclePhase.AVAILABLE
        assert pool.state("a").fail_count == 0
        assert [c.cooldown_until for c in store.calls if c.llm_name == "a"] == [None]

    asyncio.run(run())


def test_a_caller_without_a_budget_ignores_the_bound():
    """`wait=None` has nothing to compare the bound against — and is willing to wait,
    which is exactly what a slow model needs."""

    async def run():
        router, pool, _ = await _router("a", "b")
        hangs = {"a"}
        with patch(_PATCH, new=_provider(hangs)):
            with pytest.raises(NoLLMAvailableError):
                await _ask(router, wait=0.2)
            assert pool._slots["a"].unmet_budget is not None

            hangs.clear()
            result = await _ask(router)

        assert result.llm_name == "a"

    asyncio.run(run())


def test_a_comfortably_larger_budget_ignores_the_bound():
    async def run():
        router, pool, _ = await _router("a", "b")
        hangs = {"a"}
        with patch(_PATCH, new=_provider(hangs)):
            with pytest.raises(NoLLMAvailableError):
                await _ask(router, wait=0.2)
            # the bound is on record — a large budget simply does not find it binding
            assert pool._slots["a"].unmet_budget is not None
            hangs.clear()
            result = await _ask(router, wait=30.0)

        assert result.llm_name == "a"

    asyncio.run(run())


def test_wait_zero_ignores_the_bound():
    """`wait=0` bounds queueing only, so the attempt is not on a budget the model
    could miss — the bound is meaningless there."""

    async def run():
        router, _, _ = await _router("a", "b")
        hangs = {"a"}
        with patch(_PATCH, new=_provider(hangs)):
            with pytest.raises(NoLLMAvailableError):
                await _ask(router, wait=0.2)
            hangs.clear()
            result = await _ask(router, wait=0)

        assert result.llm_name == "a"

    asyncio.run(run())


def test_a_success_clears_the_bound():
    async def run():
        router, pool, _ = await _router("a", "b")
        hangs = {"a"}
        with patch(_PATCH, new=_provider(hangs)):
            with pytest.raises(NoLLMAvailableError):
                await _ask(router, wait=0.2)
            hangs.clear()
            await _ask(router, wait=30.0)  # `a` answers, which clears its bound
            assert pool._slots["a"].unmet_budget is None
            result = await _ask(router, wait=0.2)

        assert result.llm_name == "a"

    asyncio.run(run())


def test_when_nobody_can_meet_the_budget_curated_order_stands():
    """The bound is relative, so it can reorder a pool but never overturn one: flag
    everyone and the term cancels out."""

    async def run():
        router, pool, _ = await _router("a", "b")
        hangs = {"a", "b"}
        with patch(_PATCH, new=_provider(hangs)):
            with pytest.raises(NoLLMAvailableError):
                await _ask(router, wait=0.2)
            with pytest.raises(NoLLMAvailableError):
                await _ask(router, wait=0.2)
            assert pool._slots["a"].unmet_budget is not None
            assert pool._slots["b"].unmet_budget is not None

            hangs.clear()
            result = await _ask(router, wait=0.2)

        assert result.llm_name == "a"  # curated order, not "whoever was flagged last"

    asyncio.run(run())


def test_an_expiry_before_the_attempt_does_not_slander_the_model():
    """A budget already spent when the slot was taken never reached the provider —
    recording that as slowness would blame the model for the caller's clock."""

    async def run():
        router, pool, _ = await _router("a")
        with patch(_PATCH, new=_provider(set())):
            with pytest.raises(NoLLMAvailableError):
                await _ask(router, wait=-1.0)

        assert pool._slots["a"].unmet_budget is None

    asyncio.run(run())


def test_the_window_lapses():
    async def run():
        router, pool, _ = await _router("a", "b")
        hangs = {"a"}
        with (
            patch.object(pool_module, "_UNMET_WINDOW_SEC", 0.05),
            patch(_PATCH, new=_provider(hangs)),
        ):
            with pytest.raises(NoLLMAvailableError):
                await _ask(router, wait=0.2)
            hangs.clear()
            await asyncio.sleep(0.06)
            result = await _ask(router, wait=0.2)

        assert result.llm_name == "a"

    asyncio.run(run())


def test_a_lapsed_window_retires_the_bound_it_recorded():
    """Evidence the window already retired must not come back: a later, much smaller
    expiry records what it actually observed, not the stale larger number."""

    async def run():
        router, pool, _ = await _router("a", "b")
        hangs = {"a"}
        with (
            patch.object(pool_module, "_UNMET_SLACK_SEC", 0.05),
            patch(_PATCH, new=_provider(hangs)),
        ):
            with patch.object(pool_module, "_UNMET_WINDOW_SEC", 0.05):
                with pytest.raises(NoLLMAvailableError):
                    await _ask(router, wait=0.5)
                await asyncio.sleep(0.06)  # the big miss goes stale, never answered

            with pytest.raises(NoLLMAvailableError):
                await _ask(router, wait=0.1)
            assert pool._slots["a"].unmet_budget < 0.2

            hangs.clear()
            result = await _ask(router, wait=0.3)

        assert result.llm_name == "a"  # 0.3s clears the only bound still standing

    asyncio.run(run())


def test_the_bound_survives_a_config_refresh():
    """A registry resync re-adds the same slot; live routing state must not reset,
    or a resync every 60s would erase everything the pool learned."""

    async def run():
        router, pool, _ = await _router("a", "b")
        with patch(_PATCH, new=_provider({"a"})):
            with pytest.raises(NoLLMAvailableError):
                await _ask(router, wait=0.2)
        bound = pool._slots["a"].unmet_budget
        assert bound is not None

        await pool.add(_cfg("a"), None, 0)
        assert pool._slots["a"].unmet_budget == bound

    asyncio.run(run())

"""A budget expiry teaches ordering, not availability: the model that ate one
caller's whole budget stops being the first choice for equally tight ones, without
being cooled, penalised, or excluded.

The evidence is a journal row — the expiry is recorded on the row the attempt
already writes, applied to this node's pool at once, and re-derived from the tail
on every rebuild so a peer's miss reaches here too.
"""

import asyncio
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from llmbroker import chat
from llmbroker.broker import learning as learning_module
from llmbroker.broker import pool as pool_module
from llmbroker.broker.learning import Learner, budget_bounds_from_calls
from llmbroker.broker.pool import LLMPool
from llmbroker.broker.router import Router
from llmbroker.exceptions import NoLLMAvailableError
from llmbroker.models import Call, CallStatus, LifecyclePhase, LLMConfig
from llmbroker.optimizer import Optimizer

from support import make_ring

_PATCH = "llmbroker.broker.router.call_provider"
_HANG_SEC = 30


class _RecordingStore:
    """Queryable journal in a list, so a rebuild reads back what the router wrote."""

    def __init__(self) -> None:
        self.rows: list[Call] = []

    async def record(self, call: Call) -> None:
        self.rows.append(call)

    async def record_quality(self, call_id, score, *, scope=None):
        pass

    async def calls(self, *, limit: int, **_kw) -> list[Call]:
        return self.rows[::-1][:limit]


def _cfg(name: str) -> LLMConfig:
    return LLMConfig(name=name, base_url=f"https://{name}/v1", model="m", api_key_ref="K")


async def _noop_resync() -> None:
    return


async def _router(*names: str) -> tuple[Router, LLMPool, _RecordingStore]:
    """Pool in curated order: the first name given is the preferred model."""
    pool = LLMPool()
    for order, name in enumerate(names):
        await pool.add(_cfg(name), order)
    store = _RecordingStore()
    learner = Learner(Optimizer(), store, pool)
    router = Router(pool, store, learner=learner)
    # The router builds its httpx client on the first attempt, inside the caller's
    # budget: an SSL context on a cold runner costs more than the 0.2s waits below,
    # which would expire the budget before any provider is reached — a different path
    # (and no bound recorded) than what these tests are about.
    router._http_client = chat.make_client()
    return router, pool, store


def _provider(hangs: set[str]):
    """Answer at once, unless this model is in ``hangs`` — then never."""

    async def fake(config, api_key, messages, tools, *, client=None, timeout=None):  # noqa: ARG001
        if config.name in hangs:
            await asyncio.sleep(_HANG_SEC)
        return f"{config.name} answered", None, None

    return fake


async def _ask(router: Router, **kwargs):
    return await router.chat(make_ring(), [{"role": "user", "content": "hi"}], **kwargs)


_LONG_AGO = datetime(2000, 1, 1, tzinfo=UTC)


def _learner_of(router: Router) -> Learner:
    return router._learner


def _bounds(store: _RecordingStore) -> dict[str, tuple[float, datetime]]:
    return budget_bounds_from_calls(store.rows[::-1], since=_LONG_AGO)


# ── the journal row is what carries the evidence ─────────────────────────────


def test_a_budget_expiry_journals_the_budget_it_missed():
    async def run():
        router, _, store = await _router("a")
        with patch(_PATCH, new=_provider({"a"})), pytest.raises(NoLLMAvailableError):
            await _ask(router, wait=0.2)

        rows = [c for c in store.rows if c.llm_name == "a"]
        assert len(rows) == 1
        assert rows[0].budget_ms is not None
        assert 0 < rows[0].budget_ms <= 200

    asyncio.run(run())


def test_an_answered_call_journals_no_budget():
    async def run():
        router, _, store = await _router("a")
        with patch(_PATCH, new=_provider(set())):
            await _ask(router, wait=30.0)

        assert [c.budget_ms for c in store.rows] == [None]

    asyncio.run(run())


def test_an_expiry_before_the_attempt_does_not_slander_the_model():
    """A budget already spent when the slot was taken never reached the provider —
    recording that as slowness would blame the model for the caller's clock."""

    async def run():
        router, _, store = await _router("a")
        with patch(_PATCH, new=_provider(set())), pytest.raises(NoLLMAvailableError):
            await _ask(router, wait=-1.0)

        assert [c.budget_ms for c in store.rows] == [None]
        assert _bounds(store) == {}

    asyncio.run(run())


# ── deriving the bound from the tail ─────────────────────────────────────────


def _row(name: str, status: CallStatus, *, budget_ms: int | None = None) -> Call:
    return Call(
        id=str(uuid.uuid4()),
        llm_name=name,
        operation=None,
        trace_id=None,
        status=status,
        ts=datetime.now(UTC),
        budget_ms=budget_ms,
    )


def test_the_bound_is_the_largest_budget_missed():
    rows = [  # newest-first, as the tail read hands them over
        _row("a", CallStatus.ERROR, budget_ms=200),
        _row("a", CallStatus.ERROR, budget_ms=900),
        _row("a", CallStatus.ERROR, budget_ms=500),
    ]
    derived = budget_bounds_from_calls(rows, since=_LONG_AGO)
    assert derived["a"][0] == 0.9


def test_a_rated_row_is_still_weighed_for_its_missed_budget():
    """Every row of the view is a call now, score or not — the bound must not start
    depending on whether the host happened to rate the attempt."""
    rows = [
        _row("a", CallStatus.ERROR, budget_ms=200),
        replace(_row("a", CallStatus.ERROR, budget_ms=900), score=1.0),
    ]
    assert budget_bounds_from_calls(rows, since=_LONG_AGO)["a"][0] == 0.9


def test_a_success_retires_every_miss_older_than_it():
    """A model that has since answered has left the state those misses describe."""
    rows = [
        _row("a", CallStatus.ERROR, budget_ms=200),
        _row("a", CallStatus.OK),
        _row("a", CallStatus.ERROR, budget_ms=9000),
    ]
    assert budget_bounds_from_calls(rows, since=_LONG_AGO)["a"][0] == 0.2


def test_a_success_newer_than_every_miss_leaves_no_bound():
    rows = [
        _row("a", CallStatus.OK),
        _row("a", CallStatus.ERROR, budget_ms=9000),
    ]
    assert budget_bounds_from_calls(rows, since=_LONG_AGO) == {}


def test_a_stale_miss_does_not_hide_a_fresh_smaller_one():
    """The largest miss is picked before the expiry is weighed, so a lapsed big one
    left in the tail would displace a fresh small one and then expire — leaving no
    bound at all where there should be one."""
    fresh = _row("a", CallStatus.ERROR, budget_ms=200)
    stale = _row("a", CallStatus.ERROR, budget_ms=9000)
    stale = replace(stale, ts=datetime.now(UTC) - timedelta(minutes=20))
    derived = budget_bounds_from_calls(
        [fresh, stale], since=datetime.now(UTC) - timedelta(minutes=10)
    )
    assert derived["a"][0] == 0.2  # noqa: PLR2004


def test_a_rebuild_applies_the_bound_a_peer_recorded():
    """The tail is shared, so a bound another process journalled reaches this pool
    through the same rebuild that carries cooldowns."""

    async def run():
        pool = LLMPool()
        await pool.add(_cfg("a"), 0)
        await pool.add(_cfg("b"), 1)
        store = _RecordingStore()
        await store.record(_row("a", CallStatus.ERROR, budget_ms=5000))
        learner = Learner(Optimizer(), store, pool)

        await learner.relearn()

        picked = await pool.acquire(
            0, payable=frozenset({"K"}), answer_deadline=time.monotonic() + 1.0
        )
        assert picked.name == "b"

    asyncio.run(run())


def test_a_rebuild_retires_a_bound_the_tail_no_longer_carries():
    """The map is replaced wholesale: a derivation that finds no miss leaves none."""

    async def run():
        pool = LLMPool()
        await pool.add(_cfg("a"), 0)
        pool.raise_budget_bound("a", 5.0, datetime.now(UTC))
        learner = Learner(Optimizer(), _RecordingStore(), pool)

        await learner.relearn()

        assert pool._budget_bounds == {}

    asyncio.run(run())


# ── the ordering the bound buys ──────────────────────────────────────────────


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
        assert [c.cooldown_until for c in store.rows if c.llm_name == "a"] == [None]

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
            assert "a" in pool._budget_bounds

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
            assert "a" in pool._budget_bounds
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
            assert "a" not in pool._budget_bounds
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
            assert pool._budget_bounds.keys() == {"a", "b"}

            hangs.clear()
            result = await _ask(router, wait=0.2)

        assert result.llm_name == "a"  # curated order, not "whoever was flagged last"

    asyncio.run(run())


def test_the_bound_survives_a_config_refresh():
    """A registry resync re-adds the same slot; live routing state must not reset,
    or a resync every 60s would erase everything the pool learned."""

    async def run():
        router, pool, _ = await _router("a", "b")
        with patch(_PATCH, new=_provider({"a"})), pytest.raises(NoLLMAvailableError):
            await _ask(router, wait=0.2)
        bound = pool._budget_bounds["a"]

        await pool.add(_cfg("a"), 0)
        assert pool._budget_bounds["a"] == bound

    asyncio.run(run())


def test_a_dropped_slot_does_not_carry_its_bound_into_a_re_add():
    """A drop is a reset, unlike a refresh: a model withdrawn for a dead key and
    revived by a fresh secret must not inherit what the old key's calls proved."""

    async def run():
        pool = LLMPool()
        await pool.add(_cfg("a"), 0)
        await pool.add(_cfg("b"), 1)
        pool.raise_budget_bound("a", 30.0, datetime.now(UTC))

        await pool.drop("a")
        await pool.add(_cfg("a"), 0)

        picked = await pool.acquire(
            0, payable=frozenset({"K"}), answer_deadline=time.monotonic() + 5.0
        )
        assert picked.name == "a"

    asyncio.run(run())


# ── Retirement: a success, or the window lapsing ─────────────────────────────


def test_the_window_lapses():
    async def run():
        router, _, _ = await _router("a", "b")
        hangs = {"a"}
        with (
            patch.object(pool_module, "BUDGET_BOUND_WINDOW_SEC", 0.05),
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
            patch.object(pool_module, "_BUDGET_SLACK_SEC", 0.05),
            patch(_PATCH, new=_provider(hangs)),
        ):
            with patch.object(pool_module, "BUDGET_BOUND_WINDOW_SEC", 0.05):
                with pytest.raises(NoLLMAvailableError):
                    await _ask(router, wait=0.5)
                await asyncio.sleep(0.06)  # the big miss goes stale, never answered

            with pytest.raises(NoLLMAvailableError):
                await _ask(router, wait=0.1)
            assert pool._budget_bounds["a"].seconds < 0.2  # noqa: PLR2004

            hangs.clear()
            result = await _ask(router, wait=0.3)

        assert result.llm_name == "a"  # 0.3s clears the only bound still standing

    asyncio.run(run())


def test_a_model_never_picked_again_still_loses_its_bound_on_the_clock():
    """Regression: the model recovered, but every caller offers the same tight budget,
    so it is never selected and never journals a success. Without the window that
    leaves the curated favourite deprioritized for as long as the miss sits in the
    tail — an automatic verdict with no way back."""

    async def run():
        router, pool, store = await _router("a", "b")
        hangs = {"a"}
        with (
            # Both halves of the aging: the map's own expiry, and the cutoff the
            # rebuild derives with — a rebuild reading the full window would re-arm
            # what the map had already retired.
            patch.object(pool_module, "BUDGET_BOUND_WINDOW_SEC", 0.05),
            patch.object(learning_module, "BUDGET_BOUND_WINDOW_SEC", 0.05),
            patch(_PATCH, new=_provider(hangs)),
        ):
            with pytest.raises(NoLLMAvailableError):
                await _ask(router, wait=0.2)
            hangs.clear()  # `a` is healthy from here on

            first = await _ask(router, wait=0.2)
            assert first.llm_name == "b"  # the bound is standing, `a` steps aside

            await asyncio.sleep(0.06)
            # A rebuild must not re-arm what the window retired, either.
            await _learner_of(router).relearn()
            recovered = await _ask(router, wait=0.2)

        assert recovered.llm_name == "a"
        assert not any(  # `a` never answered in between, so no success cleared it
            c.llm_name == "a" and c.status is CallStatus.OK for c in store.rows[:-1]
        )

    asyncio.run(run())


# ── The bound does not depend on learning being switched on ──────────────────


def test_the_bound_applies_with_no_optimizer_and_no_learner():
    """``optimize=False`` withdraws quality learning, not the pool's own memory of
    what a call just paid for: this signal is the router's, like a cooldown."""

    async def run():
        pool = LLMPool()
        for order, name in enumerate(("a", "b")):
            await pool.add(_cfg(name), order)
        store = _RecordingStore()
        router = Router(pool, store)  # no optimizer, no learner
        router._http_client = chat.make_client()

        hangs = {"a"}
        with patch(_PATCH, new=_provider(hangs)):
            with pytest.raises(NoLLMAvailableError):
                await router.chat(make_ring(), [{"role": "user", "content": "hi"}], wait=0.2)
            hangs.clear()
            result = await router.chat(make_ring(), [{"role": "user", "content": "hi"}], wait=0.2)

        assert result.llm_name == "b"
        await router.aclose()

    asyncio.run(run())


def test_the_bound_applies_without_waiting_for_a_rebuild():
    """The rebuild is debounced and a spent budget does not force one, so the bound
    has to reach the pool from the attempt itself."""

    async def run():
        router, pool, _ = await _router("a", "b")
        learner = _learner_of(router)
        await learner.relearn()  # arm the debounce
        with patch(_PATCH, new=_provider({"a"})), pytest.raises(NoLLMAvailableError):
            await _ask(router, wait=0.2)

        assert "a" in pool._budget_bounds

    asyncio.run(run())

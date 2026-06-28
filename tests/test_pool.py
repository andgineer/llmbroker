"""Unit tests for LLMPool: slot invariants and key-resolution handling."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from llmbroker.broker.pool import LLMPool, _STORE_CACHE_TTL
from llmbroker.models import LifecyclePhase, LLMConfig, LLMState
from llmbroker.sqlite.state_store import StateStore


def _cfg(name="p1") -> LLMConfig:
    return LLMConfig(name=name, base_url="https://x/v1", model="m", api_key_ref="K")


def test_add_new_enqueues_one_slot():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), "key")
    assert pool._queue.qsize() == 1
    assert "p1" in pool


def test_add_existing_does_not_enqueue_extra_slot():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), "key")
    pool.add(_cfg(), "key2")  # same name — update, no extra queue slot
    assert pool._queue.qsize() == 1


def test_add_none_key_preserves_existing_key():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), "original")
    pool.add(_cfg(), None)  # None means "leave key intact"
    assert pool.resolved_key("p1") == "original"


def test_add_nonnone_key_overwrites_existing_key():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), "old")
    pool.add(_cfg(), "new")
    assert pool.resolved_key("p1") == "new"


def test_add_with_none_key_for_new_entry_leaves_no_key():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), None)
    assert not pool.has_key("p1")


def test_drop_removes_config_and_key():
    pool = LLMPool(state_store=None, user_id=None)
    pool.add(_cfg(), "key")
    pool.drop("p1")
    assert "p1" not in pool
    assert not pool.has_key("p1")


def test_drop_nonexistent_does_not_raise():
    pool = LLMPool(state_store=None, user_id=None)
    pool.drop("ghost")  # must be silent


def test_len_tracks_membership():
    pool = LLMPool(state_store=None, user_id=None)
    assert len(pool) == 0
    pool.add(_cfg("a"), "k")
    pool.add(_cfg("b"), "k")
    assert len(pool) == 2
    pool.drop("a")
    assert len(pool) == 1


# --- shared-state cache ---


def _make_store(states: dict[str, LLMState] | None = None, *, raise_on_read: bool = False):
    store = MagicMock()
    if raise_on_read:
        store.read = AsyncMock(side_effect=Exception("boom"))
    else:
        store.read = AsyncMock(return_value=states or {})
    store.write = AsyncMock()
    return store


def _cooling_state(seconds_ahead: float, fail_count: int = 1) -> LLMState:
    return LLMState(
        phase=LifecyclePhase.COOLING,
        cooldown_until=datetime.now(UTC) + timedelta(seconds=seconds_ahead),
        fail_count=fail_count,
    )


def test_apply_shared_cooling_no_store():
    async def run():
        pool = LLMPool(state_store=None, user_id=None)
        pool.add(_cfg(), "k")
        result = await pool.apply_shared_cooling(_cfg())
        assert result is False

    asyncio.run(run())


def test_apply_shared_cooling_store_error():
    async def run():
        store = _make_store(raise_on_read=True)
        pool = LLMPool(state_store=store, user_id=None)
        pool.add(_cfg(), "k")
        result = await pool.apply_shared_cooling(_cfg())
        assert result is False

    asyncio.run(run())


def test_apply_shared_cooling_not_in_shared():
    async def run():
        store = _make_store({})
        pool = LLMPool(state_store=store, user_id=None)
        pool.add(_cfg(), "k")
        result = await pool.apply_shared_cooling(_cfg())
        assert result is False

    asyncio.run(run())


def test_apply_shared_cooling_available():
    async def run():
        store = _make_store({"p1": LLMState(phase=LifecyclePhase.AVAILABLE)})
        pool = LLMPool(state_store=store, user_id=None)
        pool.add(_cfg(), "k")
        result = await pool.apply_shared_cooling(_cfg())
        assert result is False

    asyncio.run(run())


def test_apply_shared_cooling_cooling_no_until():
    async def run():
        store = _make_store({"p1": LLMState(phase=LifecyclePhase.COOLING, cooldown_until=None)})
        pool = LLMPool(state_store=store, user_id=None)
        pool.add(_cfg(), "k")
        result = await pool.apply_shared_cooling(_cfg())
        assert result is False

    asyncio.run(run())


def test_apply_shared_cooling_expired():
    async def run():
        store = _make_store({"p1": _cooling_state(-5)})
        pool = LLMPool(state_store=store, user_id=None)
        pool.add(_cfg(), "k")
        result = await pool.apply_shared_cooling(_cfg())
        assert result is False

    asyncio.run(run())


def test_apply_shared_cooling_active():
    async def run():
        cfg = _cfg()
        stored = _cooling_state(30)
        store = _make_store({"p1": stored})
        pool = LLMPool(state_store=store, user_id=None)
        pool.add(cfg, "k")
        cfg = await pool.acquire(0)  # mirror router: dequeue before apply

        mock_loop = MagicMock()
        with patch("llmbroker.broker.pool.asyncio.get_running_loop", return_value=mock_loop):
            result = await pool.apply_shared_cooling(cfg)

        assert result is True
        assert pool.state("p1").phase is LifecyclePhase.COOLING
        assert pool.state("p1").cooldown_until == stored.cooldown_until
        assert mock_loop.call_later.called
        call_args = mock_loop.call_later.call_args
        assert abs(call_args[0][0] - 30) < 1.0
        # callback is _reenqueue_config; calling it with the config arg should re-add the slot
        callback, cfg_arg = call_args[0][1], call_args[0][2]
        size_before = pool._queue.qsize()
        callback(cfg_arg)
        assert pool._queue.qsize() == size_before + 1

    asyncio.run(run())


def test_apply_shared_cooling_preserves_local_fail_count():
    async def run():
        cfg = _cfg()
        store = _make_store({"p1": _cooling_state(30, fail_count=2)})
        pool = LLMPool(state_store=store, user_id=None)
        pool.add(cfg, "k")
        cfg = await pool.acquire(0)  # mirror router: dequeue before apply
        # Inject local fail_count=5 via set_cooling
        pool._state.set_cooling("p1", datetime.now(UTC) + timedelta(seconds=60), 5)

        mock_loop = MagicMock()
        with patch("llmbroker.broker.pool.asyncio.get_running_loop", return_value=mock_loop):
            await pool.apply_shared_cooling(cfg)

        assert pool.state("p1").fail_count == 5

    asyncio.run(run())


def test_store_cache_hit():
    async def run():
        store = _make_store({})
        pool = LLMPool(state_store=store, user_id=None)
        await pool._get_store_cache()
        await pool._get_store_cache()
        store.read.assert_called_once()

    asyncio.run(run())


def test_store_cache_expires():
    async def run():
        store = _make_store({})
        pool = LLMPool(state_store=store, user_id=None)
        # _get_store_cache reads time.monotonic() once per call and reuses it
        # for both the hit-check and the expiry stamp.
        # Values: [call1 (miss), call2 (expired → miss)]
        mono_values = [0.0, _STORE_CACHE_TTL + 0.1]
        with patch("llmbroker.broker.pool.time.monotonic", side_effect=mono_values):
            await pool._get_store_cache()
            await pool._get_store_cache()

        assert store.read.call_count == 2

    asyncio.run(run())


def test_cool_down_invalidates_cache():
    async def run():
        store = _make_store({})
        pool = LLMPool(state_store=store, user_id=None)
        cfg = _cfg()
        pool.add(cfg, "k")

        # Warm the cache
        await pool._get_store_cache()
        assert pool._store_cache is not None

        mock_loop = MagicMock()
        with patch("llmbroker.broker.pool.asyncio.get_running_loop", return_value=mock_loop):
            headers = httpx.Headers({"retry-after": "10"})
            await pool.cool_down(cfg, headers)

        assert pool._store_cache is None

    asyncio.run(run())


def test_cross_process_cooldown_shared_via_store(tmp_path):
    async def run():
        db = tmp_path / "state.db"
        store_a = StateStore(db)
        store_b = StateStore(db)

        cfg_a = _cfg("x")
        cfg_b = _cfg("x")

        pool_a = LLMPool(state_store=store_a, user_id=None)
        pool_b = LLMPool(state_store=store_b, user_id=None)
        pool_a.add(cfg_a, "k")
        pool_b.add(cfg_b, "k")

        cfg_a = await pool_a.acquire(0)  # mirror router: dequeue before cool_down
        mock_loop = MagicMock()
        with patch("llmbroker.broker.pool.asyncio.get_running_loop", return_value=mock_loop):
            await pool_a.cool_down(cfg_a, httpx.Headers({"retry-after": "30"}))

        # Dequeue so apply_shared_cooling doesn't add a duplicate slot
        acquired = await pool_b.acquire(0)

        result = await pool_b.apply_shared_cooling(acquired)
        assert result is True
        assert pool_b.state("x").phase is LifecyclePhase.COOLING

    asyncio.run(run())


# --- SQLite cache integration ---


def test_sqlite_cache_hit_reduces_reads(tmp_path):
    """Within TTL, _get_store_cache reads SQLite only once regardless of call count."""

    async def run():
        store = StateStore(tmp_path / "state.db")
        pool = LLMPool(state_store=store, user_id=None)
        pool.add(_cfg(), "k")

        read_spy = AsyncMock(wraps=store.read)
        with patch.object(store, "read", read_spy):
            await pool._get_store_cache()  # miss → reads SQLite
            await pool._get_store_cache()  # hit → served from cache

        assert read_spy.call_count == 1

    asyncio.run(run())


def test_sqlite_cache_invalidated_after_cool_down(tmp_path):
    """After cool_down writes to SQLite and clears the cache, _get_store_cache returns fresh state."""

    async def run():
        store = StateStore(tmp_path / "state.db")
        pool = LLMPool(state_store=store, user_id=None)
        cfg = _cfg()
        pool.add(cfg, "k")

        first = await pool._get_store_cache()
        assert "p1" not in first

        mock_loop = MagicMock()
        with patch("llmbroker.broker.pool.asyncio.get_running_loop", return_value=mock_loop):
            await pool.cool_down(cfg, httpx.Headers({"retry-after": "30"}))
        assert pool._store_cache is None  # invalidated by cool_down

        fresh = await pool._get_store_cache()
        assert fresh["p1"].phase is LifecyclePhase.COOLING

    asyncio.run(run())


def test_sqlite_cache_stale_then_refreshes_after_ttl(tmp_path):
    """External write is invisible within TTL; after TTL expires the fresh state becomes visible."""

    async def run():
        db = tmp_path / "state.db"
        store_a = StateStore(db)
        store_b = StateStore(db)

        pool_a = LLMPool(state_store=store_a, user_id=None)
        pool_b = LLMPool(state_store=store_b, user_id=None)
        cfg_b = _cfg("x")
        pool_a.add(_cfg("x"), "k")
        pool_b.add(cfg_b, "k")

        # Warm pool_a's cache: SQLite is empty at this point.
        first = await pool_a._get_store_cache()
        assert "x" not in first

        # Pool B cools x, writing COOLING to SQLite. pool_a cache is not invalidated.
        mock_loop = MagicMock()
        with patch("llmbroker.broker.pool.asyncio.get_running_loop", return_value=mock_loop):
            await pool_b.cool_down(cfg_b, httpx.Headers({"retry-after": "30"}))

        # Within TTL: pool_a serves the stale (empty) cache, not yet seeing COOLING.
        within_ttl = await pool_a._get_store_cache()
        assert "x" not in within_ttl

        # Simulate TTL expiry by backdating the expiry timestamp.
        pool_a._store_cache_expires = 0.0

        # Past TTL: pool_a re-reads SQLite and now sees COOLING written by pool_b.
        past_ttl = await pool_a._get_store_cache()
        assert "x" in past_ttl
        assert past_ttl["x"].phase is LifecyclePhase.COOLING

    asyncio.run(run())

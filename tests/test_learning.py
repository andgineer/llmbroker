"""Tests for ``_LearningHook``: dead-key drops, rl_fail_count bookkeeping,
record_quality, and the debounced journal rebuild (scores, peer cooldowns,
metrics, disabled-map resync)."""

import uuid
from datetime import UTC, datetime, timedelta


from llmbroker.broker.learning import _LearningHook
from llmbroker.broker.pool import LLMPool
from llmbroker.models import Call, CallStatus, LLMConfig, key_hash
from llmbroker.optimizer import Optimizer
from llmbroker.sqlite import Store as SqliteStore
from llmbroker.standalone.store import InMemoryStore


def _cfg(name: str = "x") -> LLMConfig:
    return LLMConfig(name=name, base_url="https://x/v1", model="m", api_key_ref="k")


def _call(name: str, status: CallStatus, **kw) -> Call:
    return Call(
        id=str(uuid.uuid4()),
        llm_name=name,
        operation=kw.pop("operation", None),
        trace_id=None,
        status=status,
        ts=datetime.now(UTC),
        **kw,
    )


async def _noop_resync() -> None:
    return


def _hook(opt: Optimizer, store, pool: LLMPool) -> _LearningHook:
    return _LearningHook(opt, store, pool, _noop_resync)


# ---------------------------------------------------------------------------
# Dead-key handling (401/403 drop)
# ---------------------------------------------------------------------------


async def test_auth_failure_401_drops_llm_and_logs(caplog):
    pool = LLMPool()
    await pool.add(_cfg(), "key")
    hook = _hook(Optimizer(), InMemoryStore(), pool)

    with caplog.at_level("ERROR"):
        await hook.record(_call("x", CallStatus.ERROR, http_status=401))

    assert "x" not in pool
    assert any("API key" in r.message and "401" in r.message for r in caplog.records)


async def test_auth_failure_403_drops_llm_and_logs(caplog):
    pool = LLMPool()
    await pool.add(_cfg(), "key")
    opt = Optimizer()
    hook = _hook(opt, InMemoryStore(), pool)

    with caplog.at_level("ERROR"):
        await hook.record(_call("x", CallStatus.ERROR, http_status=403))

    assert "x" not in pool
    assert any("403" in r.message for r in caplog.records)


async def test_generic_error_does_not_drop_llm():
    pool = LLMPool()
    await pool.add(_cfg(), "key")
    opt = Optimizer()
    hook = _hook(opt, InMemoryStore(), pool)

    for _ in range(3):
        await hook.record(_call("x", CallStatus.ERROR))

    assert "x" in pool
    assert opt.rl_fail_count("x") == 3


async def test_ok_calls_reset_rl_fail_count():
    pool = LLMPool()
    await pool.add(_cfg(), "key")
    opt = Optimizer()
    hook = _hook(opt, InMemoryStore(), pool)

    await hook.record(_call("x", CallStatus.RATE_LIMITED))
    assert opt.rl_fail_count("x") == 1
    await hook.record(_call("x", CallStatus.OK))
    assert opt.rl_fail_count("x") == 0


# ---------------------------------------------------------------------------
# record_quality: self-contained, no call_id index needed
# ---------------------------------------------------------------------------


async def test_record_quality_updates_window_instantly():
    pool = LLMPool()
    await pool.add(_cfg(), "key")
    opt = Optimizer()
    hook = _hook(opt, InMemoryStore(), pool)

    await hook.record_quality("x", "summarize", 0.8)

    assert list(opt._scores[("x", "summarize")]) == [0.8]


# ---------------------------------------------------------------------------
# maybe_rebuild: quality windows + metrics from the cached tail
# ---------------------------------------------------------------------------


async def test_rebuild_loads_quality_windows_from_journal(tmp_path):
    store = SqliteStore(tmp_path / "t.db")
    pool = LLMPool()
    await pool.add(_cfg("bad"), "key")
    opt = Optimizer(quality_min_count=10, quality_floor=0.3)
    hook = _hook(opt, store, pool)

    for _ in range(10):
        await store.record_quality("bad", "summarize", 0.0)

    await hook.maybe_rebuild(force=True)

    assert opt.is_demoted("bad", "summarize") is True


async def test_rebuild_computes_metrics_cache(tmp_path):
    store = SqliteStore(tmp_path / "t.db")
    pool = LLMPool()
    await pool.add(_cfg("x"), "key")
    opt = Optimizer()
    hook = _hook(opt, store, pool)

    await store.record(_call("x", CallStatus.OK))
    await store.record(_call("x", CallStatus.OK))

    await hook.maybe_rebuild(force=True)

    assert hook.metrics_cache["x"].call_count == 2
    assert hook.metrics_cache["x"].last_status is CallStatus.OK


async def test_rebuild_is_debounced_without_force(tmp_path):
    store = SqliteStore(tmp_path / "t.db")
    pool = LLMPool()
    opt = Optimizer()
    hook = _hook(opt, store, pool)

    await hook.maybe_rebuild(force=True)
    await store.record(_call("x", CallStatus.OK))
    await hook.maybe_rebuild()  # within TTL — no-op

    assert hook.metrics_cache == {}


# ---------------------------------------------------------------------------
# Peer cooldowns: 429/401/403 gated by key hash, other failures unconditional
# ---------------------------------------------------------------------------


async def test_rebuild_applies_5xx_cooldown_unconditionally(tmp_path):
    store = SqliteStore(tmp_path / "t.db")
    pool = LLMPool()
    await pool.add(_cfg("x"), "key")  # a *different* key than the failing row below
    opt = Optimizer()
    hook = _hook(opt, store, pool)

    until = datetime.now(UTC) + timedelta(seconds=60)
    await store.record(
        _call(
            "x",
            CallStatus.ERROR,
            http_status=500,
            cooldown_until=until,
            key_hash=key_hash("someone-elses-key"),
        ),
    )

    await hook.maybe_rebuild(force=True)

    assert pool._slots["x"].cooldown_until == until


async def test_rebuild_applies_429_cooldown_only_when_key_hash_matches(tmp_path):
    store = SqliteStore(tmp_path / "t.db")
    pool = LLMPool()
    await pool.add(_cfg("x"), "shared-key")
    opt = Optimizer()
    hook = _hook(opt, store, pool)

    until = datetime.now(UTC) + timedelta(seconds=60)
    await store.record(
        _call(
            "x",
            CallStatus.RATE_LIMITED,
            http_status=429,
            cooldown_until=until,
            key_hash=key_hash("shared-key"),
        ),
    )

    await hook.maybe_rebuild(force=True)

    assert pool._slots["x"].cooldown_until == until


async def test_rebuild_ignores_429_cooldown_when_key_hash_differs(tmp_path):
    store = SqliteStore(tmp_path / "t.db")
    pool = LLMPool()
    await pool.add(_cfg("x"), "my-own-key")
    opt = Optimizer()
    hook = _hook(opt, store, pool)

    until = datetime.now(UTC) + timedelta(seconds=60)
    await store.record(
        _call(
            "x",
            CallStatus.RATE_LIMITED,
            http_status=429,
            cooldown_until=until,
            key_hash=key_hash("someone-elses-key"),
        ),
    )

    await hook.maybe_rebuild(force=True)

    assert pool._slots["x"].cooldown_until is None


async def test_rebuild_drops_dead_key_when_hash_matches(tmp_path):
    store = SqliteStore(tmp_path / "t.db")
    pool = LLMPool()
    await pool.add(_cfg("x"), "my-own-key")
    opt = Optimizer()
    hook = _hook(opt, store, pool)

    await store.record(
        _call(
            "x",
            CallStatus.ERROR,
            http_status=401,
            cooldown_until=datetime.now(UTC) + timedelta(seconds=60),
            key_hash=key_hash("my-own-key"),
        ),
    )

    await hook.maybe_rebuild(force=True)

    assert "x" not in pool


# ---------------------------------------------------------------------------
# Disabled-map resync
# ---------------------------------------------------------------------------


async def test_rebuild_seeds_and_applies_disabled_map(tmp_path):
    store = SqliteStore(tmp_path / "t.db")
    pool = LLMPool()
    await pool.add(_cfg("x"), "key")
    hook = _hook(Optimizer(), store, pool)

    await store.set_disabled("x", True)
    await hook.maybe_rebuild(force=True)

    assert pool.is_disabled("x")
    assert await store.get_disabled("x") is True


async def test_rebuild_seeds_missing_names_as_not_disabled(tmp_path):
    store = SqliteStore(tmp_path / "t.db")
    pool = LLMPool()
    await pool.add(_cfg("fresh"), "key")
    hook = _hook(Optimizer(), store, pool)

    await hook.maybe_rebuild(force=True)

    assert await store.get_disabled("fresh") is False
    assert not pool.is_disabled("fresh")


async def test_rebuild_re_enable_via_disabled_map_clears_pool_flag(tmp_path):
    store = SqliteStore(tmp_path / "t.db")
    pool = LLMPool()
    await pool.add(_cfg("x"), "key")
    pool.set_disabled("x")
    hook = _hook(Optimizer(), store, pool)

    await store.set_disabled("x", False)
    await hook.maybe_rebuild(force=True)

    assert not pool.is_disabled("x")


# ---------------------------------------------------------------------------
# resync_registry callback
# ---------------------------------------------------------------------------


async def test_maybe_rebuild_calls_resync_registry(tmp_path):
    store = SqliteStore(tmp_path / "t.db")
    pool = LLMPool()
    calls = []

    async def resync() -> None:
        calls.append(1)

    hook = _LearningHook(Optimizer(), store, pool, resync)
    await hook.maybe_rebuild(force=True)

    assert calls == [1]


async def test_maybe_rebuild_skips_resync_registry_when_disabled(tmp_path):
    """The provision-time warm start passes resync_registry=False since Catalog.provision()
    just resynced it — this must not double the registry re-read."""
    store = SqliteStore(tmp_path / "t.db")
    pool = LLMPool()
    calls = []

    async def resync() -> None:
        calls.append(1)

    hook = _LearningHook(Optimizer(), store, pool, resync)
    await hook.maybe_rebuild(force=True, resync_registry=False)

    assert calls == []

"""The unconditional refresh: a time gate decides whether to go to the network,
an identity gate decides whether what arrived changes anything.

No test sleeps — the clock is moved by writing stamps and by small intervals.
"""

import asyncio
import json
import logging
import time
import urllib.error
from unittest.mock import AsyncMock, patch

import pytest

from llmbroker.broker import presets
from llmbroker.broker.broker import AsyncBroker
from llmbroker.broker.catalog import Catalog
from llmbroker.broker.refresher import ModelListRefresher
from llmbroker.exceptions import EmptyRegistryError
from llmbroker.models import LLMConfig
from llmbroker.sqlite import Registry as SqliteRegistry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import InMemoryStore

_PRESET = (
    '[[llms]]\nname = "gemini"\nbase_url = "https://g/v1"\nmodel = "m"\napi_key_ref = "GEMINI"\n'
)
_GROWN = _PRESET + (
    '[[llms]]\nname = "groq"\nbase_url = "https://q/v1"\nmodel = "m"\napi_key_ref = "GROQ"\n'
)

_OLD = LLMConfig(
    name="old", base_url="https://x/v1", model="m", api_key_ref="GEMINI", from_preset=True
)


@pytest.fixture
def fetches(monkeypatch):
    """Counting preset stub; ``fetches.text`` is what the catalog currently serves."""

    class _Stub:
        def __init__(self) -> None:
            self.text = _PRESET
            self.names: list[str] = []

        def __call__(self, name: str) -> str:
            self.names.append(name)
            return self.text

        def __len__(self) -> int:
            return len(self.names)

    stub = _Stub()
    monkeypatch.setattr(presets, "fetch_preset_text", stub)
    return stub


@pytest.fixture
def never_fetches(monkeypatch):
    """A fetch here is the failure: the time gate should not have opened."""

    def _boom(name: str) -> str:
        raise AssertionError(f"the catalog was fetched for {name!r}")

    monkeypatch.setattr(presets, "fetch_preset_text", _boom)


def _broker(tmp_path, name: str = "b.db", **kwargs) -> AsyncBroker:
    kwargs.setdefault("secrets", DictSecrets({"GEMINI": "sk", "GROQ": "sk"}))
    kwargs.setdefault("sync", "freetier")
    kwargs.setdefault("store", InMemoryStore())
    return AsyncBroker(registry=SqliteRegistry(str(tmp_path / name)), **kwargs)


async def _seeded(tmp_path, name: str = "b.db", configs=(_OLD,)) -> str:
    db = str(tmp_path / name)
    registry = SqliteRegistry(db)
    await registry.mirror(list(configs))
    return db


def _sync_records(caplog, level: int) -> list[str]:
    """Only what the sync itself said — the pool-health alarm is the catalog's."""
    return [r.message for r in caplog.records if r.levelno >= level and "sync" in r.message]


async def _settle(broker: AsyncBroker) -> None:
    if broker._refresher._task is not None:
        await broker._refresher._task


# ── The time gate ────────────────────────────────────────────────────────────


async def test_a_started_broker_refreshes_off_the_provisioning_path(tmp_path, fetches):
    """The registry has a model_list, so the pool is provisioned from it and the refresh
    happens behind the first call, not in front of it."""
    await _seeded(tmp_path)
    broker = _broker(tmp_path)
    try:
        assert await broker.count() == 1  # provisioned from what was stored
        assert len(fetches) == 0 or broker._refresher._task is not None
        await _settle(broker)
        assert fetches.names == ["freetier"]
        assert await broker.count() == 1  # the preset's own single entry
        assert (await broker.get("gemini")).config.model == "m"
    finally:
        await broker.aclose()


async def test_the_interval_gates_every_later_call(tmp_path, fetches):
    await _seeded(tmp_path)
    broker = _broker(tmp_path, sync_interval=10_000.0)
    try:
        await broker.ensure_pool()
        await _settle(broker)
        assert len(fetches) == 1
        for _ in range(20):
            await broker.count()
        assert len(fetches) == 1
    finally:
        await broker.aclose()


async def test_a_burst_of_calls_schedules_one_refresh(tmp_path, fetches):
    await _seeded(tmp_path)
    broker = _broker(tmp_path, sync_interval=10_000.0)
    try:
        await broker.ensure_pool()
        await _settle(broker)
        broker._refresher._next_refresh = 0.0  # the interval has elapsed
        await asyncio.gather(*(broker.count() for _ in range(20)))
        await _settle(broker)
        assert len(fetches) == 2
    finally:
        await broker.aclose()


async def test_an_idle_broker_performs_no_io(tmp_path, fetches):
    """The check is lazy on activity: nothing schedules it, so a broker nobody calls
    never goes to the network a second time."""
    await _seeded(tmp_path)
    broker = _broker(tmp_path, sync_interval=0.001)
    try:
        await broker.ensure_pool()
        await _settle(broker)
        before = len(fetches)
        await asyncio.sleep(0)
        assert len(fetches) == before
    finally:
        await broker.aclose()


async def test_a_negative_interval_is_refused(tmp_path):
    with pytest.raises(ValueError, match="sync_interval"):
        _broker(tmp_path, sync_interval=-1.0)


async def test_a_zero_interval_is_refused(tmp_path):
    """Zero would leave the clock permanently due, so a serving process would refetch
    the curated list back to back. Switching the fetching off is ``None``."""
    with pytest.raises(ValueError, match="positive"):
        _broker(tmp_path, sync_interval=0)


# ── The identity gate, end to end ────────────────────────────────────────────


async def test_an_unchanged_model_list_applies_nothing_and_says_nothing(tmp_path, fetches, caplog):
    await _seeded(tmp_path)
    broker = _broker(tmp_path)
    try:
        await broker.ensure_pool()
        await _settle(broker)  # first refresh: "old" out, "gemini" in
        broker._refresher._next_refresh = 0.0
        with (
            patch.object(Catalog, "apply", new=AsyncMock()) as apply,
            caplog.at_level(logging.DEBUG, logger="llmbroker.broker"),
        ):
            await broker.count()
            await _settle(broker)
        assert apply.await_count == 0
        assert broker.last_sync_report is not None
    finally:
        await broker.aclose()
    assert any("no change" in r.message for r in caplog.records)
    assert _sync_records(caplog, logging.INFO) == []


async def test_a_moved_preset_is_routable_without_a_restart(tmp_path, fetches):
    await _seeded(tmp_path)
    broker = _broker(tmp_path)
    try:
        await broker.ensure_pool()
        await _settle(broker)
        fetches.text = _GROWN
        broker._refresher._next_refresh = 0.0
        await broker.count()
        await _settle(broker)
        assert await broker.count() == 2
        assert (await broker.get("groq")).config.base_url == "https://q/v1"
    finally:
        await broker.aclose()


# ── Failure is never the caller's problem ────────────────────────────────────


async def test_a_failed_refresh_warns_and_the_broker_keeps_serving(tmp_path, caplog, monkeypatch):
    def _fail(name: str) -> str:
        raise ValueError(f"preset {name!r} not found in catalog")

    monkeypatch.setattr(presets, "fetch_preset_text", _fail)
    await _seeded(tmp_path)
    broker = _broker(tmp_path)
    try:
        with caplog.at_level(logging.WARNING, logger="llmbroker.broker"):
            await broker.ensure_pool()
            await _settle(broker)
        assert broker._refresher._task is not None
        assert broker._refresher._task.exception() is None  # nothing surfaces as unretrieved
        assert await broker.count() == 1
    finally:
        await broker.aclose()
    assert any("refresh failed" in r.message for r in caplog.records)


async def test_a_refresh_that_raises_anything_at_all_keeps_the_broker_and_the_traceback(
    tmp_path,
    caplog,
    monkeypatch,
):
    """Two things at once, and the second is the one a broad catch usually loses.
    The refresh runs as a detached task, so an exception it does not catch stops it
    for the life of the process; but caught and flattened to one warning line, a bug
    with no message becomes unreportable — and on the start path it resurfaces as
    "registry is empty", naming the network for a cause that is not it."""

    class Boom(Exception):
        """A bug, not a network failure: carries no message at all."""

    def _fail(_name: str) -> str:
        raise Boom

    monkeypatch.setattr(presets, "fetch_preset_text", _fail)
    await _seeded(tmp_path)
    broker = _broker(tmp_path)
    try:
        with caplog.at_level(logging.WARNING, logger="llmbroker.broker"):
            await broker.ensure_pool()
            await _settle(broker)
        assert broker._refresher._task is not None
        assert broker._refresher._task.exception() is None
        assert await broker.count() == 1
    finally:
        await broker.aclose()

    unexpected = [r for r in caplog.records if "failed unexpectedly" in r.message]
    assert unexpected, "an unexpected failure must not be logged as an ordinary one"
    assert unexpected[0].levelno == logging.ERROR
    assert unexpected[0].exc_info is not None  # the traceback is what names the bug
    assert "Boom" in caplog.text


async def test_a_throttled_fetch_falls_back_to_the_cached_copy(
    tmp_path,
    caplog,
    monkeypatch,
    llmbroker_home,
):
    """A shared NAT or a busy CI runner meets the CDN's per-IP limit; that is a
    failed fetch like any other, and the installation keeps running on what it
    last saw."""
    cache = llmbroker_home / "presets"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "freetier.toml").write_text(_GROWN)

    def _throttled(*_args, **_kwargs):
        raise urllib.error.HTTPError("https://x", 429, "Too Many Requests", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(presets.urllib.request, "urlopen", _throttled)
    await _seeded(tmp_path)
    broker = _broker(tmp_path)
    try:
        with caplog.at_level(logging.DEBUG, logger="llmbroker.broker"):
            await broker.ensure_pool()
            await _settle(broker)
        assert await broker.count() == 2  # the cached model list was applied
    finally:
        await broker.aclose()
    assert any("cached copy" in r.message for r in caplog.records)
    assert _sync_records(caplog, logging.WARNING) == []


async def test_a_cold_offline_start_falls_back_to_the_bundled_preset(
    tmp_path,
    caplog,
    bundled_presets,
):
    """No network, no cache: the copy in the wheel is the floor under the chain, and
    it is what makes a first run offline provision at all."""
    broker = _broker(tmp_path)  # empty registry — the start sync must fill it
    try:
        with caplog.at_level(logging.DEBUG, logger="llmbroker.broker"):
            await broker.ensure_pool()
        assert await broker.count() > 0
    finally:
        await broker.aclose()
    assert any("bundled copy" in r.message for r in caplog.records)


async def test_the_cache_still_wins_over_the_bundled_copy(
    tmp_path, llmbroker_home, bundled_presets
):
    """The bundled preset is a floor, not a source: what the installation last saw
    upstream is newer than what its wheel was built with."""
    cache = llmbroker_home / "presets"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "freetier.toml").write_text(_GROWN)
    broker = _broker(tmp_path)
    try:
        await broker.ensure_pool()
        assert await broker.count() == 2
    finally:
        await broker.aclose()


async def test_a_successful_fetch_fills_the_cache(tmp_path, fetches, llmbroker_home):
    await _seeded(tmp_path)
    broker = _broker(tmp_path)
    try:
        await broker.ensure_pool()
        await _settle(broker)
    finally:
        await broker.aclose()
    assert (llmbroker_home / "presets" / "freetier.toml").read_text() == _PRESET


async def test_aclose_cancels_a_refresh_in_flight(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()

    async def _hang(_self, _source):
        started.set()
        await release.wait()
        raise AssertionError("the refresh should have been cancelled")

    await _seeded(tmp_path)
    broker = _broker(tmp_path)
    # The refresher's own sync, not the façade's delegate: the delegate is not what
    # the background refresh calls.
    with patch.object(ModelListRefresher, "sync", new=_hang):
        await broker.ensure_pool()
        await started.wait()
        task = broker._refresher._task
        await broker.aclose()
    assert task is not None
    assert task.cancelled()


async def test_a_refresh_in_flight_is_cancelled_before_the_ports_close(tmp_path):
    """The ordering `aclose` depends on: a refresh still running would otherwise
    write through a registry whose driver is closing."""
    events: list[str] = []
    started = asyncio.Event()

    class _ClosingRegistry:
        async def load(self):
            return [_OLD]

        async def aclose(self):
            events.append("registry closed")

    async def _hang(_self, _source):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            events.append("refresh cancelled")
            raise

    broker = AsyncBroker(
        registry=_ClosingRegistry(),
        secrets=DictSecrets({"GEMINI": "sk"}),
        store=InMemoryStore(),
        sync="freetier",
    )
    with patch.object(ModelListRefresher, "sync", new=_hang):
        await broker.ensure_pool()
        await started.wait()
        await broker.aclose()
    assert events == ["refresh cancelled", "registry closed"]


async def test_the_report_a_host_reads_is_the_one_the_refresh_recorded(tmp_path, fetches):
    """``last_sync_report`` is public surface and read-only: the façade forwards what
    the refresher recorded, whichever path recorded it."""
    await _seeded(tmp_path)
    broker = _broker(tmp_path)
    try:
        await broker.ensure_pool()
        await _settle(broker)
        assert broker.last_sync_report is broker._refresher.last_report
        assert broker.last_sync_report is not None
        with pytest.raises(AttributeError):
            broker.last_sync_report = None
    finally:
        await broker.aclose()


# ── Onboarding: an empty registry still blocks ───────────────────────────────


async def test_an_empty_registry_is_filled_before_provisioning(tmp_path, fetches):
    async with _broker(tmp_path) as broker:
        assert await broker.count() == 1
        assert broker._refresher._task is None  # the start sync was the check


async def test_an_empty_registry_with_no_catalog_still_raises(tmp_path, monkeypatch):
    def _fail(name: str) -> str:
        raise ValueError("no catalog here")

    monkeypatch.setattr(presets, "fetch_preset_text", _fail)
    broker = _broker(tmp_path)
    with pytest.raises(EmptyRegistryError):
        await broker.ensure_pool()
    await broker.aclose()


async def test_a_broker_that_follows_nothing_never_fetches(tmp_path, never_fetches):
    await _seeded(tmp_path)
    broker = _broker(tmp_path, sync=None)
    try:
        await broker.ensure_pool()
        for _ in range(5):
            await broker.count()
        assert broker._refresher._task is None
    finally:
        await broker.aclose()


# ── The stamp ────────────────────────────────────────────────────────────────


def _stamp_file(home):
    return home / "sync-stamps.json"


def _write_stamp(home, key: str, checked_at: float) -> None:
    _stamp_file(home).write_text(json.dumps({key: {"checked_at": checked_at}}))


def _key(db: str) -> str:
    return f"freetier {db}"


async def test_a_completed_check_is_recorded(tmp_path, fetches, llmbroker_home):
    db = await _seeded(tmp_path)
    broker = AsyncBroker(db, secrets=DictSecrets({"GEMINI": "sk"}), store=InMemoryStore())
    try:
        await broker.ensure_pool()
        await _settle(broker)
    finally:
        await broker.aclose()
    recorded = json.loads(_stamp_file(llmbroker_home).read_text())
    assert recorded[_key(db)]["checked_at"] == pytest.approx(time.time(), abs=60)


async def test_a_second_process_on_the_same_target_skips_the_check(
    tmp_path,
    never_fetches,
    llmbroker_home,
):
    db = await _seeded(tmp_path)
    _write_stamp(llmbroker_home, _key(db), time.time())
    broker = AsyncBroker(db, secrets=DictSecrets({"GEMINI": "sk"}), store=InMemoryStore())
    try:
        await broker.ensure_pool()
        await broker.count()
        assert broker._refresher._task is None
    finally:
        await broker.aclose()


async def test_a_second_target_is_not_gated_by_the_first(tmp_path, fetches, llmbroker_home):
    """Two projects on one machine have two model_lists to keep current."""
    first = await _seeded(tmp_path, "one.db")
    second = await _seeded(tmp_path, "two.db")
    _write_stamp(llmbroker_home, _key(first), time.time())
    broker = AsyncBroker(second, secrets=DictSecrets({"GEMINI": "sk"}), store=InMemoryStore())
    try:
        await broker.ensure_pool()
        await _settle(broker)
        assert len(fetches) == 1
    finally:
        await broker.aclose()


async def test_an_expired_stamp_checks_again(tmp_path, fetches, llmbroker_home):
    db = await _seeded(tmp_path)
    _write_stamp(llmbroker_home, _key(db), time.time() - 100_000)
    broker = AsyncBroker(db, secrets=DictSecrets({"GEMINI": "sk"}), store=InMemoryStore())
    try:
        await broker.ensure_pool()
        await _settle(broker)
        assert len(fetches) == 1
    finally:
        await broker.aclose()


async def test_a_stamp_from_the_future_counts_as_absent(tmp_path, fetches, llmbroker_home):
    """A clock moved back, or a stamp copied between hosts, must not become a gate
    that never expires."""
    db = await _seeded(tmp_path)
    _write_stamp(llmbroker_home, _key(db), time.time() + 500_000)
    broker = AsyncBroker(db, secrets=DictSecrets({"GEMINI": "sk"}), store=InMemoryStore())
    try:
        await broker.ensure_pool()
        await _settle(broker)
        assert len(fetches) == 1
    finally:
        await broker.aclose()


async def test_an_unreadable_stamp_degrades_silently(tmp_path, fetches, llmbroker_home, caplog):
    db = await _seeded(tmp_path)
    _stamp_file(llmbroker_home).write_text("{not json at all")
    broker = AsyncBroker(db, secrets=DictSecrets({"GEMINI": "sk"}), store=InMemoryStore())
    try:
        with caplog.at_level(logging.WARNING, logger="llmbroker.broker"):
            await broker.ensure_pool()
            await _settle(broker)
        assert len(fetches) == 1
    finally:
        await broker.aclose()
    assert _sync_records(caplog, logging.WARNING) == []


async def test_the_remainder_of_the_window_carries_into_the_process(
    tmp_path,
    never_fetches,
    llmbroker_home,
):
    """A process starting 90% through the window checks at the remainder, not a
    whole interval later."""
    db = await _seeded(tmp_path)
    interval = 1000.0
    _write_stamp(llmbroker_home, _key(db), time.time() - 900.0)
    broker = AsyncBroker(
        db,
        secrets=DictSecrets({"GEMINI": "sk"}),
        store=InMemoryStore(),
        sync_interval=interval,
    )
    try:
        await broker.ensure_pool()
        assert broker._refresher._next_refresh - time.monotonic() == pytest.approx(100.0, abs=5.0)
    finally:
        await broker.aclose()

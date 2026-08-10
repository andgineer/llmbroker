"""``sync_interval=None``: a process that makes no outbound connection of its own.

Every assertion is against the fetch seam rather than against timing — a refresh
that did not happen and a refresh that has not happened *yet* look the same on a
clock.
"""

import logging

import pytest

from llmbroker.broker import presets
from llmbroker.broker.broker import AsyncBroker
from llmbroker.exceptions import EmptyRegistryError
from llmbroker.models import LLMConfig
from llmbroker.sqlite import Registry as SqliteRegistry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import InMemoryStore
from llmbroker.sync import Broker

_PRESET = (
    '[[llms]]\nname = "gemini"\nbase_url = "https://g/v1"\nmodel = "m"\napi_key_ref = "GEMINI"\n'
)
_CATALOG = (
    '[[provider]]\nid="anthropic"\nbase_url="https://api.anthropic.com/v1"\n'
    'api_key_ref="ANTHROPIC_API_KEY"\nkey_help="console.anthropic.com"\n'
    '  [[provider.models]]\n  alias="opus"\n  model="claude-opus-4-8"\n'
)
_CATALOG_MOVED = _CATALOG.replace("claude-opus-4-8", "claude-opus-5")
_OWN = LLMConfig(name="own", base_url="https://x/v1", model="m", api_key_ref="GEMINI")


@pytest.fixture
def fetches(monkeypatch):
    """Counting preset stub over both bodies the catalog serves, each movable."""

    class _Stub:
        def __init__(self) -> None:
            self.names: list[str] = []
            self.catalog = _CATALOG

        def __call__(self, name: str) -> str:
            self.names.append(name)
            return {"freetier": _PRESET, "paid-catalog": self.catalog}[name]

        def __len__(self) -> int:
            return len(self.names)

    stub = _Stub()
    monkeypatch.setattr(presets, "fetch_preset_text", stub)
    return stub


@pytest.fixture
def never_fetches(monkeypatch):
    """A fetch here is the failure: this installation said it makes none."""

    def _boom(name: str) -> str:
        raise AssertionError(f"the catalog was fetched for {name!r}")

    monkeypatch.setattr(presets, "fetch_preset_text", _boom)


def _broker(tmp_path, **kwargs) -> AsyncBroker:
    kwargs.setdefault("secrets", DictSecrets({"GEMINI": "sk", "ANTHROPIC_API_KEY": "sk-ant"}))
    kwargs.setdefault("sync", "freetier")
    kwargs.setdefault("store", InMemoryStore())
    return AsyncBroker(registry=SqliteRegistry(str(tmp_path / "b.db")), **kwargs)


async def _seeded(tmp_path, configs=(_OWN,)) -> None:
    await SqliteRegistry(str(tmp_path / "b.db")).mirror(list(configs))


def _warm_catalog(home, text: str = _CATALOG) -> None:
    cache = home / "presets"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "paid-catalog.toml").write_text(text)


async def _settle(broker: AsyncBroker) -> None:
    if broker._refresher._task is not None:
        await broker._refresher._task


# ── The switch ───────────────────────────────────────────────────────────────


async def test_a_populated_registry_serves_and_nothing_is_fetched(tmp_path, never_fetches):
    await _seeded(tmp_path)
    async with _broker(tmp_path, sync_interval=None) as broker:
        for _ in range(5):
            assert await broker.count() == 1
        assert broker._refresher._task is None


async def test_an_empty_registry_raises_instead_of_being_filled(tmp_path, never_fetches):
    broker = _broker(tmp_path, sync_interval=None)
    with pytest.raises(EmptyRegistryError) as exc:
        await broker.ensure_pool()
    await broker.aclose()
    assert "await broker.sync()" in str(exc.value)  # the deploy step it is missing
    assert "network" not in str(exc.value)


async def test_the_ordinary_empty_registry_blames_neither_the_network_nor_a_fetch(
    tmp_path,
    monkeypatch,
):
    """A fetch has the cache and the wheel's copy behind it, so a network failure
    alone cannot leave the registry empty."""
    monkeypatch.setattr(presets, "fetch_preset_text", lambda _name: "")
    broker = _broker(tmp_path)
    with pytest.raises(EmptyRegistryError) as exc:
        await broker.ensure_pool()
    await broker.aclose()
    assert 'broker.sync("freetier")' in str(exc.value)
    assert "sync=None" in str(exc.value)
    assert "network" not in str(exc.value)


async def test_a_declared_alias_resolves_off_the_cache_without_fetching(
    tmp_path,
    never_fetches,
    llmbroker_home,
):
    """The paid catalog is the second clock the switch turns off. What the last
    explicit sync left in the cache is what the alias resolves through."""
    await _seeded(tmp_path)
    _warm_catalog(llmbroker_home)
    async with _broker(tmp_path, sync_interval=None, direct=["opus"]) as broker:
        cfg, _key = await broker._resolve_direct("opus")
        assert cfg.model == "claude-opus-4-8"
        assert broker._refresher._task is None


async def test_the_switch_does_not_disable_the_explicit_call(tmp_path, fetches):
    """The caller's own act, which is the whole point of the switch."""
    await _seeded(tmp_path)
    async with _broker(tmp_path, sync_interval=None) as broker:
        report = await broker.sync("freetier")
        assert report is not None
        assert report.added == ("gemini",)
        assert fetches.names == ["freetier"]
        assert await broker.count() == 2  # the host's own entry, plus the preset's


# ── The alias resolution, which is a read of its own ─────────────────────────


@pytest.fixture
def bundled_catalog(monkeypatch):
    """The wheel's copy of the paid catalog, served as the test's own body."""
    monkeypatch.setattr(
        presets,
        "bundled_preset_text",
        lambda name: _CATALOG if name == "paid-catalog" else None,
    )


async def test_a_cold_cache_resolves_from_the_bundled_copy_and_never_fetches(
    tmp_path,
    never_fetches,
    bundled_catalog,
    caplog,
):
    """The pod a deploy job cannot warm the cache for. The copy in the wheel is the
    floor this read already falls to offline; the switch reaches it without a socket."""
    await _seeded(tmp_path)
    with caplog.at_level(logging.WARNING, logger="llmbroker.broker"):
        async with _broker(tmp_path, sync_interval=None, direct=["opus"]) as broker:
            cfg, _key = await broker._resolve_direct("opus")
            assert cfg.model == "claude-opus-4-8"
    assert any("frozen at this llmbroker release" in r.message for r in caplog.records)


async def test_a_warm_cache_still_wins_over_the_bundled_copy(
    tmp_path,
    never_fetches,
    bundled_catalog,
    llmbroker_home,
):
    await _seeded(tmp_path)
    _warm_catalog(llmbroker_home, _CATALOG_MOVED)
    async with _broker(tmp_path, sync_interval=None, direct=["opus"]) as broker:
        cfg, _key = await broker._resolve_direct("opus")
        assert cfg.model == "claude-opus-5"


async def test_the_explicit_sync_is_what_moves_a_frozen_alias(
    tmp_path,
    fetches,
    bundled_catalog,
):
    """The operator took the fetching on: their own call is what unfreezes the
    resolution, and it does so inside the running process."""
    await _seeded(tmp_path)
    async with _broker(tmp_path, sync=None, sync_interval=None, direct=["opus"]) as broker:
        assert (await broker._resolve_direct("opus"))[0].model == "claude-opus-4-8"
        fetches.catalog = _CATALOG_MOVED
        assert await broker.sync() is None
        assert (await broker._resolve_direct("opus"))[0].model == "claude-opus-5"


async def test_a_sync_with_nowhere_to_keep_the_catalog_says_so_instead_of_doing_nothing(
    tmp_path,
    never_fetches,
    bundled_catalog,
    monkeypatch,
):
    """Nowhere writable and no automatic fetching: the operator's own call cannot move
    the alias, because a fresh catalog would have nowhere to live. It says which of
    the two to change rather than reporting a refresh that did not happen."""
    monkeypatch.setattr("llmbroker.broker.broker.home_dir", lambda _override: None)
    await _seeded(tmp_path)
    async with _broker(tmp_path, sync=None, sync_interval=None, direct=["opus"]) as broker:
        assert broker._home is None
        assert (await broker._resolve_direct("opus"))[0].model == "claude-opus-4-8"
        with pytest.raises(ValueError, match=r"LLMBROKER_HOME") as exc:
            await broker.sync()
    assert "sync_interval=None" in str(exc.value)


async def test_nothing_cached_and_nothing_bundled_says_what_to_run(tmp_path, never_fetches):
    await _seeded(tmp_path)
    broker = _broker(tmp_path, sync_interval=None, direct=["opus"])
    with pytest.raises(ValueError, match=r"broker\.sync\(\)") as exc:
        await broker.ensure_pool()
    await broker.aclose()
    assert "paid-catalog" in str(exc.value)


# ── The one call ─────────────────────────────────────────────────────────────


async def test_sync_with_no_argument_merges_the_preset_this_broker_follows(tmp_path, fetches):
    await _seeded(tmp_path)
    async with _broker(tmp_path, sync_interval=None) as broker:
        report = await broker.sync()
        assert report is not None
        assert report.source == "freetier"
        assert report.added == ("gemini",)
        assert fetches.names == ["freetier"]


async def test_sync_with_no_argument_refreshes_the_catalog_a_declared_alias_rides_on(
    tmp_path,
    fetches,
    llmbroker_home,
):
    """An installation that follows no preset still follows the paid catalog, and it
    is the only thing it can ask for. Nothing is merged, so there is no report."""
    await _seeded(tmp_path)
    _warm_catalog(llmbroker_home)
    async with _broker(tmp_path, sync=None, sync_interval=None, direct=["opus"]) as broker:
        assert await broker.sync() is None
        assert fetches.names == ["paid-catalog"]
        assert [c.name for c in await broker._registry.load()] == ["own"]


async def test_sync_with_no_argument_follows_nothing_where_there_is_nothing_to_follow(
    tmp_path,
    never_fetches,
):
    await _seeded(tmp_path)
    async with _broker(tmp_path, sync=None, sync_interval=None) as broker:
        assert await broker.sync() is None


async def test_the_sync_wrapper_takes_the_same_call(tmp_path, fetches):
    """The blocking façade forwards the whole signature, default included."""
    await _seeded(tmp_path)
    with Broker(
        registry=SqliteRegistry(str(tmp_path / "b.db")),
        secrets=DictSecrets({"GEMINI": "sk"}),
        store=InMemoryStore(),
        sync="freetier",
        sync_interval=None,
    ) as broker:
        report = broker.sync()
        assert report is not None
        assert report.added == ("gemini",)


async def test_the_default_still_arms_the_clock(tmp_path, fetches):
    """Unstated, nothing changes: the clock arms and a call past the interval
    schedules the refresh."""
    await _seeded(tmp_path)
    async with _broker(tmp_path, sync_interval=10_000.0) as broker:
        await _settle(broker)
        assert fetches.names == ["freetier"]
        broker._refresher._next_refresh = 0.0
        await broker.count()
        await _settle(broker)
        assert fetches.names == ["freetier", "freetier"]

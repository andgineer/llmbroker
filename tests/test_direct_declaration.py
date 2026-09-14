"""``direct=``: paid models declared in code, resolved at provision and never stored.

The paid catalog is served through the fetch seam; nothing here goes to the
network, and the wheel's own copy stays invisible unless a test asks for it.
"""

import asyncio
import logging
import threading

import pytest

from llmbroker.broker import presets
from llmbroker.broker.broker import AsyncBroker
from llmbroker.broker.catalog import Catalog
from llmbroker.broker.curated import curated_providers
from llmbroker.broker.presets import PresetSource
from llmbroker.broker.refresher import ModelListRefresher
from llmbroker.broker.stamps import write_stamp
from llmbroker.exceptions import MissingKeyError, UnknownModelError
from llmbroker.models import LLMConfig
from llmbroker.sqlite import Registry as SqliteRegistry
from llmbroker.sqlite import Secrets as SqliteSecrets
from llmbroker.standalone.registry import Registry as FileRegistry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import InMemoryStore

_PRESET = (
    '[[llms]]\nname = "gemini"\nbase_url = "https://g/v1"\nmodel = "m"\napi_key_ref = "GEMINI"\n'
)
_CATALOG = (
    '[[provider]]\nid="anthropic"\nbase_url="https://api.anthropic.com/v1"\n'
    'api_key_ref="ANTHROPIC_API_KEY"\nkey_help="console.anthropic.com"\n'
    '  [[provider.models]]\n  alias="opus"\n  model="claude-opus-4-8"\n'
    '  [[provider.models]]\n  alias="sonnet"\n  model="claude-sonnet-5"\n'
)
_CATALOG_MOVED = _CATALOG.replace("claude-opus-4-8", "claude-opus-5")

_SECRETS = {"GEMINI": "sk-gem", "ANTHROPIC_API_KEY": "sk-ant"}


@pytest.fixture
def served(monkeypatch):
    """The two bodies the catalog serves, each movable on its own."""
    bodies = {"freetier": _PRESET, "paid-catalog": _CATALOG}
    monkeypatch.setattr(presets, "fetch_preset_text", lambda name: bodies[name])
    return bodies


async def _settle(broker: AsyncBroker) -> None:
    if broker._refresher._task is not None:
        await broker._refresher._task


async def _resolved(broker: AsyncBroker, alias: str) -> LLMConfig:
    """What `direct()` would call — the resolved entry, without opening a client."""
    cfg, _key = await broker.llms.resolve_direct(alias)
    return cfg


def _broker(tmp_path, **kwargs) -> AsyncBroker:
    kwargs.setdefault("secrets", DictSecrets(dict(_SECRETS)))
    kwargs.setdefault("sync", "freetier")
    kwargs.setdefault("store", InMemoryStore())
    target = tmp_path / "llms.toml"
    if not target.exists():
        target.write_text("")
    return AsyncBroker(registry=FileRegistry(target), home=tmp_path / "home", **kwargs)


async def test_a_declared_alias_is_reachable_and_absent_from_the_pool(tmp_path, served):
    async with _broker(tmp_path, direct=["opus"]) as broker:
        cfg = await _resolved(broker, "opus")
        assert cfg.model == "claude-opus-4-8"
        assert cfg.name == "anthropic-claude-opus-4-8"
        assert cfg.base_url == "https://api.anthropic.com/v1"
        # Rule 1: only the curated model list is routed.
        assert await broker.count() == 1
        assert set(await broker.snapshot()) == {"gemini"}


async def test_a_declared_alias_follows_the_catalog_on_the_refresh_clock(tmp_path, served):
    """Nothing pins the version: once the refresh has moved the cached catalog the
    next resolution follows it — inside the running process, and in the next one."""
    async with _broker(tmp_path, direct=["opus"], sync_interval=0.001) as broker:
        assert (await _resolved(broker, "opus")).model == "claude-opus-4-8"
        await _settle(broker)  # the refresh that direct() fired on the clock
        served["paid-catalog"] = _CATALOG_MOVED
        broker._refresher._next_refresh = 0.0
        await broker.count()
        await _settle(broker)
        assert (await _resolved(broker, "opus")).model == "claude-opus-5"

    async with _broker(tmp_path, direct=["opus"], sync=None) as restarted:
        assert (await _resolved(restarted, "opus")).model == "claude-opus-5"


async def test_a_re_resolution_logs_the_version_it_moved_to(tmp_path, served, caplog):
    """A version move is the only notice a deployment gets that `direct("opus")` now
    answers from a different model. The first resolution has nothing to compare."""
    with caplog.at_level(logging.INFO, logger="llmbroker.broker"):
        async with _broker(tmp_path, direct=["opus"], sync_interval=0.001) as broker:
            await _resolved(broker, "opus")
            await _settle(broker)
            assert [r.message for r in caplog.records if r.message.startswith("direct=:")] == []
            served["paid-catalog"] = _CATALOG_MOVED
            broker._refresher._next_refresh = 0.0
            await broker.count()
            await _settle(broker)
            await _resolved(broker, "opus")
    assert [r.message for r in caplog.records if r.message.startswith("direct=:")] == [
        "direct=: opus: claude-opus-4-8 -> claude-opus-5",
    ]


async def test_nothing_declared_is_ever_written_to_the_registry(tmp_path, served):
    target = tmp_path / "llms.toml"
    async with _broker(tmp_path, direct=["opus"]) as broker:
        await broker.count()
    assert "opus" not in target.read_text()
    assert {c.name for c in await FileRegistry(target).load()} == {"gemini"}


async def test_a_declared_config_is_used_verbatim_and_no_refresh_touches_it(tmp_path, served):
    mine = LLMConfig(
        name="my-gateway",
        base_url="https://gateway.internal/v1",
        model="pinned-1",
        api_key_ref="GATEWAY_KEY",
    )
    secrets = DictSecrets({**_SECRETS, "GATEWAY_KEY": "sk-gw"})
    async with _broker(tmp_path, direct=[mine], secrets=secrets) as broker:
        cfg, _key = await broker.llms.resolve_direct(name="my-gateway")
        assert (cfg.model, cfg.base_url) == ("pinned-1", "https://gateway.internal/v1")
        assert set(await broker.snapshot()) == {"gemini"}


async def test_a_declared_config_is_never_pooled(tmp_path, served):
    """Pool membership is not a field: the pool is exactly what the registry holds."""
    mine = LLMConfig(name="mine", base_url="https://m/v1", model="m", api_key_ref="GEMINI")
    async with _broker(tmp_path, direct=[mine]) as broker:
        assert set(await broker.snapshot()) == {"gemini"}


async def test_a_typo_raises_at_provision_and_lists_the_aliases(tmp_path, served):
    broker = _broker(tmp_path, direct=["opus-5"])
    with pytest.raises(UnknownModelError, match="available aliases: opus, sonnet"):
        await broker.ensure_pool()
    await broker.aclose()


async def test_a_declared_alias_colliding_with_the_registry_names_both(tmp_path, served):
    target = tmp_path / "llms.toml"
    target.write_text(
        '[[llms]]\nname="anthropic-claude-opus-4-8"\n'
        'model="claude-opus-4-8"\nbase_url="https://api.anthropic.com/v1"\n'
        'api_key_ref="ANTHROPIC_API_KEY"\n',
    )
    broker = _broker(tmp_path, direct=["opus"], sync=None)
    with pytest.raises(ValueError, match="the registry already carries"):
        await broker.ensure_pool()
    await broker.aclose()


async def test_the_same_alias_declared_twice_blames_direct_and_not_the_registry(
    tmp_path,
    served,
):
    """Both entries came from the constructor call; naming the registry would send
    the reader to a file that has nothing to do with it."""
    broker = _broker(tmp_path, direct=["opus", "opus"], sync=None)
    with pytest.raises(ValueError, match="declares the alias 'opus' twice"):
        await broker.ensure_pool()
    await broker.aclose()


async def test_a_declared_model_with_no_key_exists_and_says_so(tmp_path, served):
    async with _broker(tmp_path, direct=["opus"], secrets=DictSecrets({"GEMINI": "sk"})) as broker:
        with pytest.raises(MissingKeyError, match="ANTHROPIC_API_KEY"):
            await broker.direct("opus")
        assert await broker.count() == 1  # the pool is untouched by the missing key


async def test_a_declared_model_alone_is_a_configured_installation(tmp_path, served):
    """A broker that follows no model list and declares one paid model has no pool —
    that is the shape `direct=` makes ordinary, not an empty registry."""
    async with _broker(tmp_path, direct=["opus"], sync=None) as broker:
        assert await broker.count() == 0
        assert (await _resolved(broker, "opus")).model == "claude-opus-4-8"


async def test_a_declared_alias_is_not_pinned_to_the_catalog_in_the_wheel(
    tmp_path,
    served,
    monkeypatch,
):
    """The bundled copy is a floor, never preferred over a fetch. Ahead of one it
    would hold a declared alias on the version this release shipped with for as
    long as the release stayed installed."""
    monkeypatch.setattr(presets, "bundled_preset_text", lambda _name: _CATALOG)
    served["paid-catalog"] = _CATALOG_MOVED
    async with _broker(tmp_path, direct=["opus"], sync=None) as broker:
        assert (await _resolved(broker, "opus")).model == "claude-opus-5"


async def test_an_unreachable_catalog_never_moves_a_declared_alias_backwards(
    tmp_path,
    served,
    monkeypatch,
):
    """Nowhere writable is a supported deployment, and there the re-resolution has
    no cache to read. Falling through to the wheel's copy would silently move a
    working alias back to the version this release shipped with — the paid half of
    the rule the stored entries already follow."""
    monkeypatch.setattr("llmbroker.broker.broker.home_dir", lambda _override: None)
    monkeypatch.setattr(presets, "bundled_preset_text", lambda _name: _CATALOG)
    served["paid-catalog"] = _CATALOG_MOVED
    async with _broker(tmp_path, direct=["opus"], sync=None) as broker:
        assert broker._home is None
        assert (await _resolved(broker, "opus")).model == "claude-opus-5"

        def _offline(_name: str) -> str:
            raise ValueError("offline")

        monkeypatch.setattr(presets, "fetch_preset_text", _offline)
        broker._catalog.invalidate_declared()
        assert (await _resolved(broker, "opus")).model == "claude-opus-5"


async def test_an_alias_the_catalog_dropped_keeps_serving_and_does_not_stop_the_refresh(
    tmp_path,
    served,
    caplog,
):
    """A catalog that moved out from under a running process is not the caller's
    error. Raising there killed the refresh task — silently, since a detached task
    swallows it — so a model list change stopped reaching the live pool for the life of
    the process, and calls the previous resolution served fine started failing."""
    async with _broker(tmp_path, direct=["opus"], sync_interval=0.001) as broker:
        await broker.ensure_pool()
        await _settle(broker)
        assert (await _resolved(broker, "opus")).model == "claude-opus-4-8"
        await _settle(broker)

        served["paid-catalog"] = _CATALOG.replace(
            '  [[provider.models]]\n  alias="opus"\n  model="claude-opus-4-8"\n',
            "",
        )
        served["freetier"] = _PRESET + (
            '[[llms]]\nname = "groq"\nbase_url = "https://q/v1"\nmodel = "m"\n'
            'api_key_ref = "GEMINI"\n'
        )
        broker._refresher._next_refresh = 0.0
        with caplog.at_level(logging.WARNING, logger="llmbroker.broker"):
            await broker.count()
            await _settle(broker)

        # The model list change reached the live pool: the refresh ran to the end.
        assert set(await broker.snapshot()) == {"gemini", "groq"}
        # And the declared model still answers, on the last resolution that worked.
        assert (await _resolved(broker, "opus")).model == "claude-opus-4-8"

    assert any("stay on the resolution already in use" in r.message for r in caplog.records)


async def test_the_catalog_is_refreshed_where_no_model_list_is_synced(tmp_path, served):
    """``sync=None`` is a real shape — a registry a deploy job fills. The catalog a
    declared alias resolves through still has to move, so it carries its own clock
    rather than riding on the model list's."""
    async with _broker(tmp_path, direct=["opus"], sync=None, sync_interval=0.001) as broker:
        await broker.ensure_pool()
        await _settle(broker)
        assert (await _resolved(broker, "opus")).model == "claude-opus-4-8"
        await _settle(broker)  # at this interval, that resolution was due a refresh too
        served["paid-catalog"] = _CATALOG_MOVED
        broker._refresher._next_refresh = 0.0  # the period has elapsed
        await broker.count()  # the clock, armed by the broker itself, is due
        await _settle(broker)
        assert (await _resolved(broker, "opus")).model == "claude-opus-5"


async def test_direct_resolves_on_the_refresh_clock_and_not_per_call(
    tmp_path,
    served,
    monkeypatch,
):
    """``direct()`` is a request path. Re-resolving there would put a catalog read
    and a TOML parse under every call to a declared model."""
    async with _broker(tmp_path, direct=["opus"], sync=None) as broker:
        assert (await _resolved(broker, "opus")).model == "claude-opus-4-8"
        await _settle(broker)  # the catalog refresh that first call fired
        assert (await _resolved(broker, "opus")).model == "claude-opus-4-8"

        def _boom(_self, _name, **_kwargs) -> str:
            raise AssertionError("direct() re-read the paid catalog")

        monkeypatch.setattr(PresetSource, "text", _boom)
        for _ in range(5):
            assert (await _resolved(broker, "opus")).model == "claude-opus-4-8"


async def test_direct_calls_alone_move_a_declared_alias_once_the_interval_elapses(
    tmp_path,
    served,
):
    """A host that only calls models by name has no other call to carry the clock, so
    without ``direct()`` ticking it an alias would never be re-resolved after start."""
    async with _broker(tmp_path, direct=["opus"], sync=None, sync_interval=3600) as broker:
        assert (await _resolved(broker, "opus")).model == "claude-opus-4-8"
        await _settle(broker)
        served["paid-catalog"] = _CATALOG_MOVED
        for _ in range(3):  # inside the interval: nothing more is fetched
            assert (await _resolved(broker, "opus")).model == "claude-opus-4-8"
        assert broker._refresher._task.done()

        broker._refresher._next_refresh = 0.0  # the interval has elapsed
        await _resolved(broker, "opus")
        await _settle(broker)
        assert (await _resolved(broker, "opus")).model == "claude-opus-5"
        assert broker._provisioned is False


async def test_direct_never_fills_an_empty_registry_on_its_own_path(tmp_path, served, monkeypatch):
    """The blocking fill belongs to provisioning: a ``direct()`` call answers while the
    model list it does not route over is still being fetched, if it is fetched at all."""
    release = threading.Event()

    def slow(name: str) -> str:
        if name == "freetier":
            release.wait(5)
        return served[name]

    monkeypatch.setattr(presets, "fetch_preset_text", slow)
    async with _broker(tmp_path, direct=["opus"]) as broker:
        try:
            cfg = await asyncio.wait_for(_resolved(broker, "opus"), 2)
            assert cfg.model == "claude-opus-4-8"
            assert await FileRegistry(tmp_path / "llms.toml").load() == []
            assert broker._refresher._attempted is False
        finally:
            release.set()
        await _settle(broker)
        assert broker._provisioned is False


async def test_a_direct_call_that_armed_the_clock_first_still_lets_provisioning_fill(
    tmp_path,
    served,
):
    """Arming is not the start decision: a registry left empty after ``direct()`` armed
    the clock is still filled by the first call that provisions."""
    broker = _broker(tmp_path, direct=["opus"])
    # A peer checked this target a moment ago, so the tick arms without firing.
    write_stamp(broker._home, broker._refresher._stamp_key("freetier"))
    try:
        await _resolved(broker, "opus")
        assert broker._refresher._task is None
        assert await broker.count() == 1
    finally:
        await broker.aclose()


async def test_a_refresh_fired_by_direct_rebuilds_no_pool(tmp_path, served, monkeypatch):
    """No pool exists to rebuild, and building one would report the health of a pool
    this host never routes over."""
    rebuilds: list[object] = []
    real = Catalog.rebuild

    async def counted(self, known=None):
        rebuilds.append(known)
        await real(self, known)

    monkeypatch.setattr(Catalog, "rebuild", counted)
    async with _broker(tmp_path, direct=["opus"]) as broker:
        await _resolved(broker, "opus")
        await _settle(broker)
        # The refresh ran — it synced the list this broker follows — and rebuilt nothing.
        assert broker.last_sync_report is not None
        assert rebuilds == []
        assert broker._pool.configs == {}
        await broker.count()
        assert len(rebuilds) == 1


_NEWCOMER = (
    '[[llms]]\nname = "newcomer"\nbase_url = "https://n/v1"\nmodel = "m"\napi_key_ref = "GEMINI"\n'
)


async def test_a_registry_write_landing_mid_provisioning_still_reaches_the_pool(
    tmp_path,
    served,
    monkeypatch,
):
    """The interleaving forced here: provisioning reads the registry, a refresh ``direct()``
    fired writes it and reaches its rebuild step, then provisioning finishes. Once both are
    done the pool holds what the registry holds."""
    async with _broker(tmp_path) as seeding:
        await seeding.sync()
    served["freetier"] = _PRESET + _NEWCOMER

    fetch_released = threading.Event()
    refresh_rebuilding = asyncio.Event()
    provisioning_read: list[bool] = []

    def gated(name: str) -> str:
        if name == "freetier":
            fetch_released.wait(5)
        return served[name]

    real_rebuild = Catalog.rebuild

    async def paused_after_the_first_read(self, known=None):
        await real_rebuild(self, known)
        if not provisioning_read:
            provisioning_read.append(True)
            fetch_released.set()
            await asyncio.wait_for(refresh_rebuilding.wait(), 5)

    real_rebuild_pool = ModelListRefresher._rebuild_pool

    async def marked(self):
        refresh_rebuilding.set()
        await real_rebuild_pool(self)

    monkeypatch.setattr(presets, "fetch_preset_text", gated)
    monkeypatch.setattr(Catalog, "rebuild", paused_after_the_first_read)
    monkeypatch.setattr(ModelListRefresher, "_rebuild_pool", marked)
    try:
        async with _broker(tmp_path, direct=["opus"]) as broker:
            await _resolved(broker, "opus")  # arms the clock on the seeding broker's stamp
            broker._refresher._next_refresh = 0.0  # the interval has elapsed
            await _resolved(broker, "opus")  # fires the refresh, held at its fetch
            await broker.count()
            await _settle(broker)
            assert await FileRegistry(tmp_path / "llms.toml").load() != []
            assert set(broker._pool.configs) == {"gemini", "newcomer"}
    finally:
        fetch_released.set()


async def test_a_refresh_costs_one_catalog_read_however_many_calls_are_in_flight(
    tmp_path,
    served,
    monkeypatch,
):
    """The refresh drops the resolution, and the callers that find it gone are
    requests. Without one read for all of them every call in flight would parse the
    catalog for itself — and where nothing is writable, fetch it for itself."""
    async with _broker(tmp_path, direct=["opus"], sync=None) as broker:
        await broker.ensure_pool()
        await _settle(broker)  # the start-up catalog refresh, out of the way
        reads: list[object] = []
        real = PresetSource.text

        def counted(self, name, **kwargs) -> str:
            reads.append(name)
            return real(self, name, **kwargs)

        monkeypatch.setattr(PresetSource, "text", counted)
        broker._catalog.invalidate_declared()
        resolved = await asyncio.gather(*(_resolved(broker, "opus") for _ in range(50)))

    assert len(reads) == 1
    assert {cfg.model for cfg in resolved} == {"claude-opus-4-8"}


async def test_a_declared_models_key_reaches_a_writable_secrets_backend(
    tmp_path,
    served,
    monkeypatch,
):
    """The bootstrap a stored model list gets from `sync`, applied to a model that is
    never stored. Without it `direct=` is dead wherever secrets live in a backend
    rather than the environment."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    db = str(tmp_path / "reg.db")
    await SqliteRegistry(db).mirror([])
    secrets = SqliteSecrets(db)
    broker = AsyncBroker(
        registry=SqliteRegistry(db),
        secrets=secrets,
        store=InMemoryStore(),
        home=tmp_path / "home",
        direct=["opus"],
        sync=None,
    )
    try:
        _cfg, key = await broker.llms.resolve_direct("opus")
        assert key == "sk-ant-from-env"
        assert await secrets.resolve("ANTHROPIC_API_KEY") == "sk-ant-from-env"
    finally:
        await broker.aclose()
        await secrets.aclose()


async def test_a_keyless_declared_model_reaches_the_snapshot_with_its_help(tmp_path, served):
    """The catalog knows where the key comes from, and nothing stores a declared
    model — so if that help is not carried out of the resolution it is lost. It is
    reported apart from the pool's own missing keys: this model is never routed."""
    async with _broker(tmp_path, direct=["opus"], secrets=DictSecrets({"GEMINI": "sk"})) as broker:
        snapshot = await broker.snapshot()

    assert [k.api_key_ref for k in snapshot.direct_missing_keys] == ["ANTHROPIC_API_KEY"]
    assert snapshot.direct_missing_keys[0].help == "console.anthropic.com"
    # The handle the caller passes to direct(), not the version-carrying name.
    assert snapshot.direct_missing_keys[0].entry_names == ("opus",)
    # A model the pool never routes cannot hold a pool key back.
    assert snapshot.missing_keys == ()


async def test_the_missing_key_error_says_where_to_get_the_key(tmp_path, served):
    async with _broker(tmp_path, direct=["opus"], secrets=DictSecrets({"GEMINI": "sk"})) as broker:
        with pytest.raises(MissingKeyError, match="console.anthropic.com"):
            await broker.direct("opus")


async def test_a_missing_key_is_logged_once_with_its_help(tmp_path, served, caplog):
    """At broker creation, where a host reading the log can still act on it — and
    once per ref, not once per reconcile, or a standing gap fills the log."""
    with caplog.at_level(logging.INFO, logger="llmbroker.broker"):
        async with _broker(
            tmp_path,
            direct=["opus"],
            secrets=DictSecrets({"GEMINI": "sk"}),
        ) as broker:
            await broker.count()
            await broker.rebuild()

    lines = [r.message for r in caplog.records if "ANTHROPIC_API_KEY" in r.message]
    assert len(lines) == 1
    assert "console.anthropic.com" in lines[0]


async def test_a_registry_written_help_wins_over_the_catalogs(tmp_path, served):
    """A host that wrote its own `[keys]` hint meant it; ours is the fallback."""
    target = tmp_path / "llms.toml"
    target.write_text(_PRESET + '[keys.ANTHROPIC_API_KEY]\nhelp = "ask ops for it"\n')
    async with _broker(tmp_path, direct=["opus"], secrets=DictSecrets({"GEMINI": "sk"})) as broker:
        snapshot = await broker.snapshot()

    assert snapshot.direct_missing_keys[0].help == "ask ops for it"


async def test_a_collision_appearing_later_does_not_fail_a_call(tmp_path, served, caplog):
    """The collision check runs wherever the overlay is read, and one of those places
    is the rebuild an exhausted pool triggers. An edit landing under a running broker
    is a supported event; it must not turn an exhausted pool into an exception."""
    target = tmp_path / "llms.toml"
    target.write_text(_PRESET)
    async with _broker(tmp_path, direct=["opus"], sync=None) as broker:
        await broker.ensure_pool()
        target.write_text(
            _PRESET + '[[llms]]\nname="anthropic-claude-opus-4-8"\n'
            'model="claude-opus-4-8"\nbase_url="https://api.anthropic.com/v1"\n'
            'api_key_ref="ANTHROPIC_API_KEY"\n',
        )
        with caplog.at_level(logging.ERROR, logger="llmbroker.broker"):
            await broker._rebuild_safely("pool exhaustion")
        assert await broker.count() == 1

    assert any("pool rebuild on pool exhaustion failed" in r.message for r in caplog.records)


async def test_resolution_reads_the_cached_catalog_rather_than_the_network(tmp_path, served):
    """A provision must not depend on the network: the sync warmed the cache, and
    the resolution reads it."""
    async with _broker(tmp_path, direct=["opus"]) as broker:
        await broker.count()

    def _boom(name: str) -> str:
        raise AssertionError(f"provision fetched {name!r} instead of reading the cache")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(presets, "fetch_preset_text", _boom)
        async with _broker(tmp_path, direct=["opus"], sync=None) as second:
            assert (await _resolved(second, "opus")).model == "claude-opus-4-8"


async def test_the_shipped_openai_aliases_resolve_with_what_their_tool_calls_need(
    tmp_path,
    bundled_presets,
):
    aliases = ["gpt", "gpt-mini", "gpt-fast"]
    secrets = DictSecrets({"OPENAI_API_KEY": "sk-openai"})
    async with _broker(tmp_path, direct=aliases, sync=None, secrets=secrets) as broker:
        for alias in aliases:
            assert (await _resolved(broker, alias)).tool_params == {"reasoning_effort": "none"}


async def test_a_curated_provider_declares_a_model_the_catalog_does_not_carry(tmp_path, served):
    """The whole path the curated reader exists for: read the catalog as data with no
    broker, build a declaration for a model it never listed, and reach it by name."""
    home = tmp_path / "home"
    (home / "presets").mkdir(parents=True)
    (home / "presets" / "paid-catalog.toml").write_text(_CATALOG, encoding="utf-8")

    provider = curated_providers(home=home)[0]
    declared = provider.declare("claude-unreleased-9")
    assert declared.name == "anthropic-claude-unreleased-9"

    async with _broker(tmp_path, direct=[declared]) as broker:
        cfg, key = await broker.llms.resolve_direct(name="anthropic-claude-unreleased-9")
        assert cfg.model == "claude-unreleased-9"
        assert cfg.base_url == "https://api.anthropic.com/v1"
        assert key == "sk-ant"
        assert await broker.count() == 1  # the pool holds the synced entry only

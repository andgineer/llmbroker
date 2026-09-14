"""``Broker()`` with no arguments: the curated pool, keys from the environment,
everything llmbroker remembers in the home directory.

No test goes to the network — the preset body is served through the fetch seam,
and the wheel's own copy is only visible where a test asks for it.
"""

import logging

import pytest

from llmbroker.broker import presets
from llmbroker.broker.broker import AsyncBroker
from llmbroker.exceptions import EmptyRegistryError
from llmbroker.home import HOME_ENV_VAR
from llmbroker.standalone.registry import Registry as FileRegistry
from llmbroker.standalone.store import FileStore, InMemoryStore
from llmbroker.sync import Broker

_PRESET = (
    '[[llms]]\nname = "gemini"\nbase_url = "https://g/v1"\nmodel = "m"\napi_key_ref = "GEMINI"\n'
    '[[llms]]\nname = "groq"\nbase_url = "https://q/v1"\nmodel = "m"\napi_key_ref = "GROQ"\n'
)


@pytest.fixture
def fetches(monkeypatch):
    """Counting preset stub — the only way a model list reaches a fileless broker."""

    class _Stub:
        def __init__(self) -> None:
            self.names: list[str] = []

        def __call__(self, name: str) -> str:
            self.names.append(name)
            return _PRESET

    stub = _Stub()
    monkeypatch.setattr(presets, "fetch_preset_text", stub)
    return stub


async def _settle(broker: AsyncBroker) -> None:
    if broker._refresher._task is not None:
        await broker._refresher._task


async def test_a_cold_start_provisions_and_writes_its_model_list(fetches, llmbroker_home):
    async with AsyncBroker() as broker:
        assert await broker.count() == 2
    assert fetches.names == ["freetier"]
    model_list = llmbroker_home / "model-list.toml"
    assert model_list.is_file()
    assert {c.name for c in await FileRegistry(model_list).load()} == {"gemini", "groq"}


async def test_a_second_run_reads_what_the_first_wrote_and_does_not_fetch(
    fetches,
    llmbroker_home,
):
    """The stamp gates the check and the model list file carries the pool, so a
    short-lived script pays one round trip per interval, not per invocation."""
    async with AsyncBroker() as first:
        await first.count()
    async with AsyncBroker() as second:
        assert await second.count() == 2
        await _settle(second)
    assert fetches.names == ["freetier"]


async def test_a_cold_offline_start_provisions_from_the_bundled_preset(
    llmbroker_home,
    bundled_presets,
):
    """No network and no cache is the state of every first run behind a firewall;
    the preset in the wheel is what makes it work anyway."""
    async with AsyncBroker() as broker:
        assert await broker.count() > 0


async def test_nowhere_writable_still_routes_and_simply_forgets(
    fetches,
    monkeypatch,
    tmp_path,
):
    """A read-only container is a supported deployment: the model list and the journal
    live in memory for that run."""
    monkeypatch.setattr("llmbroker.broker.broker.home_dir", lambda _override: None)
    async with AsyncBroker() as broker:
        assert await broker.count() == 2
        assert broker._home is None
        assert isinstance(broker._store, InMemoryStore)
    assert list(tmp_path.iterdir()) == []


async def test_the_journal_is_one_per_home_not_one_per_working_directory(
    fetches,
    llmbroker_home,
):
    async with AsyncBroker() as broker:
        await broker.ensure_pool()
        assert isinstance(broker._store, FileStore)
    assert (llmbroker_home / "store").is_dir()


async def test_two_homes_share_no_model_list_and_no_journal(fetches, tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    async with AsyncBroker(home=one) as first, AsyncBroker(home=two) as second:
        assert await first.count() == 2
        assert await second.count() == 2
    assert (one / "model-list.toml").is_file()
    assert (two / "model-list.toml").is_file()
    assert (one / "store").is_dir()
    assert (two / "store").is_dir()
    # Each home carries its own check record, so neither gates the other.
    assert fetches.names == ["freetier", "freetier"]


async def test_keys_come_from_the_environment_and_the_working_directory_env(
    fetches,
    monkeypatch,
    tmp_path,
):
    """Where a script's keys are. An exported variable always wins over the file."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("GEMINI=from-file\nGROQ=from-file\n")
    monkeypatch.setenv("GROQ", "from-env")
    async with AsyncBroker(home=tmp_path / "home") as broker:
        await broker.ensure_pool()
        assert broker._pool.config("gemini").api_key_ref in broker._catalog.payable
        assert (
            await broker._shared_ring.resolve(broker._pool.config("groq").api_key_ref) == "from-env"
        )


async def test_a_home_override_beats_the_environment_variable(fetches, monkeypatch, tmp_path):
    monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "env-home"))
    async with AsyncBroker(home=tmp_path / "explicit") as broker:
        await broker.count()
    assert (tmp_path / "explicit" / "model-list.toml").is_file()
    assert not (tmp_path / "env-home").exists()


async def test_entering_and_leaving_fetches_nothing_and_raises_nothing(fetches, llmbroker_home):
    """The fill waits for the first call that needs a pool, so entering and leaving a broker
    pay no round trip. A ``direct()`` call is not free of one: on the default sync source it
    syncs the list on the clock, in the background."""
    async with AsyncBroker():
        pass
    with Broker():
        pass
    assert fetches.names == []
    assert not (llmbroker_home / "model-list.toml").exists()


_PAID_CATALOG = (
    '[[provider]]\nid="anthropic"\nbase_url="https://api.anthropic.com/v1"\n'
    'api_key_ref="ANTHROPIC_API_KEY"\n'
    '  [[provider.models]]\n  alias="opus"\n  model="claude-opus-4-8"\n'
)


async def test_a_direct_only_host_on_the_default_sync_source_never_provisions_the_pool(
    caplog,
    llmbroker_home,
    monkeypatch,
    tmp_path,
):
    """It follows the free list like any default broker, so a due ``direct()`` syncs the
    registry in the background; that sync builds no pool, and so logs no health of one."""
    fetched: list[str] = []
    bodies = {"freetier": _PRESET, "paid-catalog": _PAID_CATALOG}

    def fetch(name: str) -> str:
        fetched.append(name)
        return bodies[name]

    monkeypatch.setattr(presets, "fetch_preset_text", fetch)
    monkeypatch.chdir(tmp_path)  # no working-directory .env: the free pool stays keyless
    for ref in ("GEMINI", "GROQ"):
        monkeypatch.delenv(ref, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    with caplog.at_level(logging.INFO, logger="llmbroker"):
        async with AsyncBroker(direct=["opus"]) as broker:
            assert broker._refresher._task is None
            await broker.direct("opus")  # no check record yet: the clock is due at once
            await _settle(broker)
            assert "freetier" in fetched
            stored = await FileRegistry(llmbroker_home / "model-list.toml").load()
            assert {c.name for c in stored} == {"gemini", "groq"}
            await broker.direct("opus")
            assert broker._provisioned is False
            assert broker._pool.configs == {}
    assert [r.message for r in caplog.records if r.message.startswith("pool ")] == []


async def test_an_unfillable_model_list_raises_at_the_first_routed_call(
    llmbroker_home,
    monkeypatch,
):
    def _fail(name: str) -> str:
        raise ValueError(f"preset {name!r} not found in catalog")

    monkeypatch.setattr(presets, "fetch_preset_text", _fail)
    async with AsyncBroker() as broker:
        with pytest.raises(EmptyRegistryError, match="registry is empty"):
            await broker.ask("hi", wait=0)
        with pytest.raises(EmptyRegistryError, match="registry is empty"):
            await broker.ensure_pool()


async def test_a_model_list_that_cannot_be_filled_says_what_to_do(
    caplog, llmbroker_home, monkeypatch
):
    """Offline, no cache, nothing bundled: the empty-registry error is the one a
    host is expected to catch, and it names the sync that would fill it."""

    def _fail(name: str) -> str:
        raise ValueError(f"preset {name!r} not found in catalog")

    monkeypatch.setattr(presets, "fetch_preset_text", _fail)
    broker = AsyncBroker()
    with caplog.at_level(logging.WARNING, logger="llmbroker.broker"), pytest.raises(Exception) as e:
        await broker.ensure_pool()
    await broker.aclose()
    assert "registry is empty" in str(e.value)
    assert 'broker.sync("freetier")' in str(e.value)
    # The fill that failed said why, at WARNING; the error itself may not blame the
    # network, which the cache and the wheel's copy sit behind.
    assert "network" not in str(e.value)
    assert any("not found in catalog" in r.message for r in caplog.records)


async def test_a_cold_offline_start_says_the_model_list_is_not_the_current_one(
    caplog,
    llmbroker_home,
    bundled_presets,
    monkeypatch,
):
    """The wheel's copy is frozen at the installed release. Serving it silently is
    what makes `preset > llms.toml` hand someone a stale model list they then keep."""

    def _fail(name: str) -> str:
        raise ValueError(f"preset {name!r} not found in catalog")

    monkeypatch.setattr(presets, "fetch_preset_text", _fail)
    with caplog.at_level(logging.WARNING, logger="llmbroker.broker"):
        async with AsyncBroker() as broker:
            assert await broker.count() > 0
    assert any("frozen at this llmbroker release" in r.message for r in caplog.records)

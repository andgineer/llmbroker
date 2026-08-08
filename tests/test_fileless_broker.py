"""``Broker()`` with no arguments: the curated pool, keys from the environment,
everything llmbroker remembers in the home directory.

No test goes to the network — the preset body is served through the fetch seam,
and the wheel's own copy is only visible where a test asks for it.
"""

import logging

import pytest

from llmbroker.broker import presets
from llmbroker.broker.broker import AsyncBroker
from llmbroker.home import HOME_ENV_VAR
from llmbroker.standalone.registry import Registry as FileRegistry
from llmbroker.standalone.store import FileStore, InMemoryStore

_PRESET = (
    '[[llms]]\nname = "gemini"\nbase_url = "https://g/v1"\nmodel = "m"\napi_key_ref = "GEMINI"\n'
    '[[llms]]\nname = "groq"\nbase_url = "https://q/v1"\nmodel = "m"\napi_key_ref = "GROQ"\n'
)


@pytest.fixture
def fetches(monkeypatch):
    """Counting preset stub — the only way a lineup reaches a fileless broker."""

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


async def test_a_cold_start_provisions_and_writes_its_lineup(fetches, llmbroker_home):
    async with AsyncBroker() as broker:
        assert await broker.count() == 2
    assert fetches.names == ["freetier"]
    lineup = llmbroker_home / "lineup.toml"
    assert lineup.is_file()
    assert {c.name for c in await FileRegistry(lineup).load()} == {"gemini", "groq"}


async def test_a_second_run_reads_what_the_first_wrote_and_does_not_fetch(
    fetches,
    llmbroker_home,
):
    """The stamp gates the check and the lineup file carries the pool, so a
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
    """A read-only container is a supported deployment: the lineup and the journal
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
        assert isinstance(broker._store, FileStore)
    assert (llmbroker_home / "store").is_dir()


async def test_two_homes_share_no_lineup_and_no_journal(fetches, tmp_path):
    one, two = tmp_path / "one", tmp_path / "two"
    async with AsyncBroker(home=one) as first, AsyncBroker(home=two) as second:
        assert await first.count() == 2
        assert await second.count() == 2
    assert (one / "lineup.toml").is_file()
    assert (two / "lineup.toml").is_file()
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
        assert broker._pool.has_key("gemini")
        assert broker._pool.resolved_key("groq") == "from-env"


async def test_a_home_override_beats_the_environment_variable(fetches, monkeypatch, tmp_path):
    monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "env-home"))
    async with AsyncBroker(home=tmp_path / "explicit") as broker:
        await broker.count()
    assert (tmp_path / "explicit" / "lineup.toml").is_file()
    assert not (tmp_path / "env-home").exists()


async def test_a_lineup_that_cannot_be_filled_says_what_to_do(caplog, llmbroker_home, monkeypatch):
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
    # A fileless broker got here *through* the default sync, so telling it to turn
    # that same sync on names the thing that just failed.
    assert "none could be fetched" in str(e.value)
    assert "network access" in str(e.value)


async def test_a_cold_offline_start_says_the_lineup_is_not_the_current_one(
    caplog,
    llmbroker_home,
    bundled_presets,
    monkeypatch,
):
    """The wheel's copy is frozen at the installed release. Serving it silently is
    what makes `preset > llms.toml` hand someone a stale lineup they then keep."""

    def _fail(name: str) -> str:
        raise ValueError(f"preset {name!r} not found in catalog")

    monkeypatch.setattr(presets, "fetch_preset_text", _fail)
    with caplog.at_level(logging.WARNING, logger="llmbroker.broker"):
        async with AsyncBroker() as broker:
            assert await broker.count() > 0
    assert any("frozen at this llmbroker release" in r.message for r in caplog.records)

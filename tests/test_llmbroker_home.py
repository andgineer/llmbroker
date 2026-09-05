"""The home directory: first writable candidate wins, and nowhere writable is a
supported outcome rather than a failure."""

import tempfile
from pathlib import Path

import pytest

from llmbroker import home
from llmbroker.broker.broker import AsyncBroker
from llmbroker.home import HOME_ENV_VAR, home_dir, home_dir_for_read
from llmbroker.sqlite import Registry as SqliteRegistry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import InMemoryStore


@pytest.fixture(autouse=True)
def _no_probe_cache(monkeypatch):
    """The writability answer is cached for the life of a process; each case here
    builds a different filesystem, so it must start from an empty cache."""
    monkeypatch.setattr(home, "_probed", {})


def test_the_env_var_wins_over_the_platform_directory(tmp_path, monkeypatch):
    monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "env-home"))
    assert home_dir() == tmp_path / "env-home"


def test_an_override_wins_over_the_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "env-home"))
    assert home_dir(tmp_path / "mine") == tmp_path / "mine"


def test_the_directory_is_created(tmp_path):
    target = tmp_path / "deep" / "nested"
    assert home_dir(target) == target
    assert target.is_dir()


def test_the_probe_file_does_not_survive(tmp_path):
    home_dir(tmp_path / "h")
    assert list((tmp_path / "h").iterdir()) == []


def test_an_unwritable_candidate_falls_through(tmp_path, monkeypatch):
    """A read-only mount, a container running as `nobody`: the next candidate takes
    over rather than the broker failing. Blocked here with a plain file in the way,
    which no privilege level can write through."""
    (tmp_path / "blocker").write_text("not a directory")
    fallback = tmp_path / "fallback"
    monkeypatch.setenv(HOME_ENV_VAR, str(fallback))
    assert home_dir(tmp_path / "blocker" / "home") == fallback


def test_nothing_writable_yields_none(monkeypatch):
    monkeypatch.setattr(home, "_is_writable", lambda _path: False)
    assert home_dir() is None


def test_the_last_resort_is_a_per_user_temp_directory(monkeypatch):
    monkeypatch.delenv(HOME_ENV_VAR, raising=False)
    monkeypatch.setattr(home, "_platform_cache_dir", lambda: None)
    resolved = home_dir()
    assert resolved is not None
    assert resolved.parent == Path(tempfile.gettempdir())
    assert resolved.name.startswith("llmbroker-")


def test_the_platform_directory_is_used_when_no_env_var_is_set(tmp_path, monkeypatch):
    monkeypatch.delenv(HOME_ENV_VAR, raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert home_dir() == tmp_path / "xdg" / "llmbroker"


def test_a_read_takes_the_named_directory_unprobed(tmp_path, monkeypatch):
    monkeypatch.setattr(home, "_is_writable", lambda _path: False)
    assert home_dir_for_read(tmp_path / "mounted") == tmp_path / "mounted"


def test_a_read_with_nothing_named_takes_the_home_directory(tmp_path, monkeypatch):
    monkeypatch.setenv(HOME_ENV_VAR, str(tmp_path / "env-home"))
    assert home_dir_for_read() == tmp_path / "env-home"


def _broker(tmp_path, **kwargs) -> AsyncBroker:
    kwargs.setdefault("sync", None)
    return AsyncBroker(
        registry=SqliteRegistry(str(tmp_path / "b.db")),
        secrets=DictSecrets({}),
        store=InMemoryStore(),
        **kwargs,
    )


async def test_home_isolates_two_brokers(tmp_path):
    one = _broker(tmp_path, home=tmp_path / "one")
    two = _broker(tmp_path, home=tmp_path / "two")
    try:
        assert one._home == tmp_path / "one"
        assert two._home == tmp_path / "two"
    finally:
        await one.aclose()
        await two.aclose()


async def test_a_broker_still_runs_with_no_writable_home(tmp_path, monkeypatch):
    monkeypatch.setattr(home, "_is_writable", lambda _path: False)
    await SqliteRegistry(str(tmp_path / "b.db")).mirror([])
    broker = _broker(tmp_path)
    try:
        assert broker._home is None
    finally:
        await broker.aclose()

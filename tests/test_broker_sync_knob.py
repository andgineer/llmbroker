"""The ``sync=`` constructor knob: refresh once before provisioning, never raise."""

import logging

import pytest

from llmbroker.broker import upstream
from llmbroker.broker.broker import AsyncBroker
from llmbroker.exceptions import EmptyRegistryError, SyncRefusedError
from llmbroker.models import LLMConfig, SyncReport
from llmbroker.sqlite import Registry as SqliteRegistry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import InMemoryStore
from llmbroker.sync import Broker

_PRESET = (
    '[[llms]]\nname = "gemini"\nbase_url = "https://g/v1"\nmodel = "m"\napi_key_ref = "GEMINI"\n'
)


@pytest.fixture
def preset(monkeypatch):
    monkeypatch.setattr(upstream, "fetch_preset_text", lambda _name: _PRESET)


def _lineup(tmp_path):
    src = tmp_path / "lineup.toml"
    src.write_text(_PRESET)
    return str(src)


def _broker(tmp_path, **kwargs):
    kwargs.setdefault("secrets", DictSecrets({"GEMINI": "sk"}))
    return AsyncBroker(
        registry=SqliteRegistry(str(tmp_path / "b.db")),
        store=InMemoryStore(),
        **kwargs,
    )


async def test_the_knob_populates_a_fresh_registry_before_provisioning(tmp_path):
    """Without it the same broker cannot even open: provisioning an empty registry
    raises, and `async with` provisions on entry."""
    async with _broker(tmp_path, sync=_lineup(tmp_path)) as broker:
        assert await broker.count() == 1

    without = tmp_path / "without"
    without.mkdir()
    with pytest.raises(EmptyRegistryError):
        async with _broker(without):
            pass


async def test_a_preset_name_works_the_same_way(tmp_path, preset):
    async with _broker(tmp_path, sync="freetier") as broker:
        assert await broker.count() == 1
        assert broker.last_sync_report.added == ("gemini",)


async def test_the_knob_runs_for_a_caller_that_never_enters_the_context_manager(tmp_path):
    broker = _broker(tmp_path, sync=_lineup(tmp_path))
    try:
        assert await broker.count() == 1  # ensure_pool via a plain public call
    finally:
        await broker.aclose()


async def test_the_fetch_is_attempted_once_across_repeated_calls(tmp_path, monkeypatch):
    calls: list[str] = []

    def counted(name):
        calls.append(name)
        return _PRESET

    monkeypatch.setattr(upstream, "fetch_preset_text", counted)
    async with _broker(tmp_path, sync="freetier") as broker:
        await broker.count()
        await broker.count()
    assert calls == ["freetier"]


async def test_a_failed_provision_does_not_re_fetch(tmp_path, monkeypatch):
    """The knob is guarded by its own flag, not by the provisioned flag, so a retry
    after an empty-registry failure does not go back to the network."""
    calls: list[str] = []

    def counted(name):
        calls.append(name)
        return ""  # an empty lineup leaves the fresh registry empty

    monkeypatch.setattr(upstream, "fetch_preset_text", counted)
    broker = _broker(tmp_path, sync="freetier")
    for _ in range(2):
        with pytest.raises(EmptyRegistryError):
            await broker.ensure_pool()
    await broker.aclose()
    assert calls == ["freetier"]


async def test_a_fetch_failure_keeps_the_existing_config_and_logs(tmp_path, monkeypatch, caplog):
    def fail(_name):
        raise ValueError("preset 'freetier' not found in catalog")

    monkeypatch.setattr(upstream, "fetch_preset_text", fail)
    db = str(tmp_path / "b.db")
    await SqliteRegistry(db).mirror(
        [LLMConfig(name="old", base_url="https://x/v1", model="m", api_key_ref="GEMINI")],
    )
    with caplog.at_level(logging.WARNING, logger="llmbroker.broker"):
        async with _broker(tmp_path, sync="freetier") as broker:
            assert await broker.count() == 1
            assert (await broker.get("old")).config.name == "old"
            assert broker.last_sync_report is None
    assert any("not found in catalog" in r.message for r in caplog.records)


async def test_a_refusal_stashes_the_report_and_continues(tmp_path, monkeypatch, caplog):
    refused = SyncReport(source="freetier", applied=False, kept=("old",))

    async def refuse(_self, _source):
        raise SyncRefusedError("refused", report=refused)

    monkeypatch.setattr(AsyncBroker, "sync", refuse)
    db = str(tmp_path / "b.db")
    await SqliteRegistry(db).mirror(
        [LLMConfig(name="old", base_url="https://x/v1", model="m", api_key_ref="GEMINI")],
    )
    with caplog.at_level(logging.WARNING, logger="llmbroker.broker"):
        async with _broker(tmp_path, sync="freetier") as broker:
            assert await broker.count() == 1
            assert broker.last_sync_report is refused
    assert any("refused" in r.message for r in caplog.records)


async def test_an_explicit_sync_still_raises(tmp_path, monkeypatch):
    """The knob's leniency is the knob's alone."""

    def fail(_name):
        raise ValueError("preset 'freetier' not found in catalog")

    monkeypatch.setattr(upstream, "fetch_preset_text", fail)
    db = str(tmp_path / "b.db")
    await SqliteRegistry(db).mirror(
        [LLMConfig(name="old", base_url="https://x/v1", model="m", api_key_ref="GEMINI")],
    )
    broker = _broker(tmp_path)
    with pytest.raises(ValueError, match="not found in catalog"):
        await broker.sync("freetier")
    await broker.aclose()


def test_the_sync_wrapper_takes_the_same_knob(tmp_path):
    broker = Broker(
        registry=SqliteRegistry(str(tmp_path / "b.db")),
        store=InMemoryStore(),
        secrets=DictSecrets({"GEMINI": "sk"}),
        sync=_lineup(tmp_path),
    )
    with broker:
        assert broker.count() == 1
        assert broker.last_sync_report.added == ("gemini",)

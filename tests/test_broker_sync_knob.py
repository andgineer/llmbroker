"""The ``sync=`` constructor argument: which curated preset this broker follows, and
the guarantee that following it never breaks a start.

An empty registry is filled before provisioning, blocking, because provisioning an
empty registry raises. A registry that already has a model list is provisioned from
what it holds and refreshed afterwards, off the request path — so a test that reads
what the refresh did waits for it with ``_settle``.
"""

import http.client
import logging

import pytest

from llmbroker.broker import presets
from llmbroker.broker.broker import AsyncBroker
from llmbroker.broker.refresher import ModelListRefresher
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
    monkeypatch.setattr(presets, "fetch_preset_text", lambda _name: _PRESET)


def _broker(tmp_path, **kwargs):
    kwargs.setdefault("secrets", DictSecrets({"GEMINI": "sk"}))
    kwargs.setdefault("sync", "freetier")
    return AsyncBroker(
        registry=SqliteRegistry(str(tmp_path / "b.db")),
        store=InMemoryStore(),
        **kwargs,
    )


async def _settle(broker) -> None:
    """Wait out the background refresh a non-empty registry schedules."""
    if broker._refresher._task is not None:
        await broker._refresher._task


async def test_the_knob_populates_a_fresh_registry_before_provisioning(tmp_path, preset):
    """Without it the same broker cannot even open: provisioning an empty registry
    raises, and `async with` provisions on entry."""
    async with _broker(tmp_path, sync="freetier") as broker:
        assert await broker.count() == 1
        assert broker.last_sync_report.added == ("gemini",)

    without = tmp_path / "without"
    without.mkdir()
    with pytest.raises(EmptyRegistryError):
        async with _broker(without, sync=None):
            pass


async def test_the_knob_runs_for_a_caller_that_never_enters_the_context_manager(tmp_path, preset):
    broker = _broker(tmp_path, sync="freetier")
    try:
        assert await broker.count() == 1  # ensure_pool via a plain public call
    finally:
        await broker.aclose()


async def test_the_fetch_is_attempted_once_across_repeated_calls(tmp_path, monkeypatch):
    calls: list[str] = []

    def counted(name):
        calls.append(name)
        return _PRESET

    monkeypatch.setattr(presets, "fetch_preset_text", counted)
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
        return ""  # an empty model list leaves the fresh registry empty

    monkeypatch.setattr(presets, "fetch_preset_text", counted)
    broker = _broker(tmp_path, sync="freetier")
    for _ in range(2):
        with pytest.raises(EmptyRegistryError):
            await broker.ensure_pool()
    await broker.aclose()
    assert calls == ["freetier"]


async def test_a_fetch_failure_keeps_the_existing_config_and_logs(tmp_path, monkeypatch, caplog):
    def fail(_name):
        raise ValueError("preset 'freetier' not found in catalog")

    monkeypatch.setattr(presets, "fetch_preset_text", fail)
    db = str(tmp_path / "b.db")
    await SqliteRegistry(db).mirror(
        [LLMConfig(name="old", base_url="https://x/v1", model="m", api_key_ref="GEMINI")],
    )
    with caplog.at_level(logging.WARNING, logger="llmbroker.broker"):
        async with _broker(tmp_path, sync="freetier") as broker:
            await _settle(broker)
            assert await broker.count() == 1
            assert (await broker.get("old")).config.name == "old"
            assert broker.last_sync_report is None
    assert any("not found in catalog" in r.message for r in caplog.records)


async def test_a_truncated_response_does_not_stop_the_broker_from_starting(
    tmp_path,
    monkeypatch,
    caplog,
):
    """IncompleteRead is an HTTPException, not an OSError — unwrapped it escaped the
    knob's own except and killed process start, the one thing the knob prevents."""

    class _Truncated:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            raise http.client.IncompleteRead(b"partial")

    monkeypatch.setattr(presets.urllib.request, "urlopen", lambda *a, **k: _Truncated())
    db = str(tmp_path / "b.db")
    await SqliteRegistry(db).mirror(
        [LLMConfig(name="old", base_url="https://x/v1", model="m", api_key_ref="GEMINI")],
    )
    with caplog.at_level(logging.WARNING, logger="llmbroker.broker"):
        async with _broker(tmp_path, sync="freetier") as broker:
            await _settle(broker)
            assert await broker.count() == 1
    assert any("failed reading" in r.message for r in caplog.records)


async def test_a_refusal_stashes_the_report_and_continues(tmp_path, monkeypatch, caplog):
    refused = SyncReport(source="freetier", applied=False, removed=("old",))

    async def refuse(_self, _source):
        raise SyncRefusedError("refused", report=refused)

    monkeypatch.setattr(ModelListRefresher, "sync", refuse)
    db = str(tmp_path / "b.db")
    await SqliteRegistry(db).mirror(
        [LLMConfig(name="old", base_url="https://x/v1", model="m", api_key_ref="GEMINI")],
    )
    with caplog.at_level(logging.WARNING, logger="llmbroker.broker"):
        async with _broker(tmp_path, sync="freetier") as broker:
            await _settle(broker)
            assert await broker.count() == 1
            assert broker.last_sync_report is refused
    assert any("refused" in r.message for r in caplog.records)


async def test_an_explicit_sync_still_raises(tmp_path, monkeypatch):
    """The knob's leniency is the knob's alone."""

    def fail(_name):
        raise ValueError("preset 'freetier' not found in catalog")

    monkeypatch.setattr(presets, "fetch_preset_text", fail)
    db = str(tmp_path / "b.db")
    await SqliteRegistry(db).mirror(
        [LLMConfig(name="old", base_url="https://x/v1", model="m", api_key_ref="GEMINI")],
    )
    broker = _broker(tmp_path)
    with pytest.raises(ValueError, match="not found in catalog"):
        await broker.sync("freetier")
    await broker.aclose()


# ── What an unstated sync= defaults to ───────────────────────────────────────


def test_a_bare_broker_follows_the_curated_preset(tmp_path):
    broker = AsyncBroker(home=tmp_path)
    assert broker._refresher._source == "freetier"


def test_a_connection_string_follows_the_curated_preset(tmp_path):
    """The installation is still llmbroker's — the string only moved its storage."""
    broker = AsyncBroker(str(tmp_path / "b.db"), store=InMemoryStore())
    assert broker._refresher._source == "freetier"


def test_a_registry_object_must_state_what_it_follows(tmp_path):
    with pytest.raises(ValueError, match="sync=") as exc:
        AsyncBroker(registry=SqliteRegistry(str(tmp_path / "b.db")), store=InMemoryStore())
    assert "'freetier'" in str(exc.value)
    assert "sync=None" in str(exc.value)


def test_the_sync_wrapper_refuses_the_same_way(tmp_path):
    with pytest.raises(ValueError, match="sync="):
        Broker(registry=SqliteRegistry(str(tmp_path / "b.db")), store=InMemoryStore())


def test_a_registry_object_with_an_explicit_preset_is_accepted(tmp_path, preset):
    broker = _broker(tmp_path, sync="freetier")
    assert broker._refresher._source == "freetier"


async def test_sync_none_over_a_registry_object_never_goes_to_the_network(tmp_path, monkeypatch):
    def _boom(name: str) -> str:
        raise AssertionError(f"the catalog was fetched for {name!r}")

    monkeypatch.setattr(presets, "fetch_preset_text", _boom)
    db = str(tmp_path / "b.db")
    await SqliteRegistry(db).mirror(
        [LLMConfig(name="own", base_url="https://x/v1", model="m", api_key_ref="GEMINI")],
    )
    async with _broker(tmp_path, sync=None) as broker:
        await broker.count()
        await _settle(broker)


def test_the_sync_wrapper_takes_the_same_knob(tmp_path, preset):
    broker = Broker(
        registry=SqliteRegistry(str(tmp_path / "b.db")),
        store=InMemoryStore(),
        secrets=DictSecrets({"GEMINI": "sk"}),
        sync="freetier",
    )
    with broker:
        assert broker.count() == 1
        assert broker.last_sync_report.added == ("gemini",)

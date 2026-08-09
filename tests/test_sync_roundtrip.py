"""The documented DB-init workflow end to end: the deploy job syncs the curated
preset into the DB registry from the host's own broker, then a broker opened on
that DB alone routes a call.

The sync half and the serving half run as two separate brokers, exactly as a
release-phase job and the app process would.
"""

from unittest.mock import AsyncMock, patch

import pytest

from llmbroker.broker import presets
from llmbroker.broker.broker import AsyncBroker
from llmbroker.standalone.secrets import DictSecrets

_PATCH = "llmbroker.broker.router.call_provider"

_PRESET = (
    '[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="ROUNDTRIP_KEY"\n'
    '[[llms]]\nname="p2"\nbase_url="https://y/v1"\nmodel="m"\napi_key_ref="ROUNDTRIP_KEY"\n'
)


@pytest.fixture(autouse=True)
def served(monkeypatch):
    """What the catalog serves for ``sync("freetier")``; a test rolls it forward."""
    state = {"text": _PRESET}
    monkeypatch.setattr(presets, "fetch_preset_text", lambda _name: state["text"])
    return state


async def _sync_into(db: str, served, body: str = _PRESET):
    """The deploy-job half: build a broker, sync, close — never `async with`, which
    would provision the still-empty registry first."""
    served["text"] = body
    broker = AsyncBroker(db)
    try:
        return await broker.sync("freetier")
    finally:
        await broker.aclose()


async def test_sync_then_broker_on_the_db_answers(tmp_path, monkeypatch, served):
    db = str(tmp_path / "broker.db")
    monkeypatch.setenv("ROUNDTRIP_KEY", "sk-test")

    report = await _sync_into(db, served)
    assert report.added == ("p1", "p2")

    async with AsyncBroker(db) as broker:
        assert await broker.count() == 2
        with patch(_PATCH, new=AsyncMock(return_value=("ok", None, None))):
            result = await broker.ask("hi")
    assert result.text == "ok"
    assert result.llm_name == "p1"


async def test_sync_bootstraps_the_key_into_the_db_secrets(tmp_path, monkeypatch, served):
    """The sync persists an env-resolvable key, so a later process needs no env var."""
    db = str(tmp_path / "broker.db")
    monkeypatch.setenv("ROUNDTRIP_KEY", "sk-persisted")
    await _sync_into(db, served)
    monkeypatch.delenv("ROUNDTRIP_KEY")

    async with AsyncBroker(db) as broker:
        assert broker._pool.resolved_key("p1") == "sk-persisted"


_TWO_PROVIDERS = (
    '[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="RETIRED_KEY"\n'
    '[[llms]]\nname="p2"\nbase_url="https://y/v1"\nmodel="m"\napi_key_ref="ROUNDTRIP_KEY"\n'
)


async def test_a_retired_provider_whose_key_is_here_keeps_routing(tmp_path, monkeypatch, served):
    """The pool does not shrink while the models still work — the entry stays callable
    and the report names it on this run and every later one."""
    db = str(tmp_path / "broker.db")
    monkeypatch.setenv("ROUNDTRIP_KEY", "sk-test")
    monkeypatch.setenv("RETIRED_KEY", "sk-retired")
    await _sync_into(db, served, _TWO_PROVIDERS)

    shrunk = (
        '[[llms]]\nname="p2"\nbase_url="https://y/v1"\nmodel="m"\napi_key_ref="ROUNDTRIP_KEY"\n'
    )
    report = await _sync_into(db, served, shrunk)
    assert (report.kept, report.kept_refs, report.removed) == (("p1",), ("RETIRED_KEY",), ())

    secrets = DictSecrets({"ROUNDTRIP_KEY": "sk-test", "RETIRED_KEY": "sk-retired"})
    async with AsyncBroker(db, secrets=secrets) as broker:
        assert await broker.count() == 2
        assert "p1" in broker._pool


async def test_a_retired_provider_without_a_key_here_is_dropped(tmp_path, monkeypatch, served):
    """The other half of the same rule: nothing is lost, because nothing could be
    called through that ref in the first place."""
    db = str(tmp_path / "broker.db")
    monkeypatch.setenv("ROUNDTRIP_KEY", "sk-test")
    monkeypatch.delenv("RETIRED_KEY", raising=False)
    await _sync_into(db, served, _TWO_PROVIDERS)

    shrunk = (
        '[[llms]]\nname="p2"\nbase_url="https://y/v1"\nmodel="m"\napi_key_ref="ROUNDTRIP_KEY"\n'
    )
    report = await _sync_into(db, served, shrunk)
    # No key was ever here, so there is none to revoke either.
    assert (report.removed, report.kept, report.orphan_refs) == (("p1",), (), ())


async def test_a_replacement_carrying_the_same_ref_removes_the_old_entry(
    tmp_path, monkeypatch, served
):
    db = str(tmp_path / "broker.db")
    monkeypatch.setenv("ROUNDTRIP_KEY", "sk-test")
    await _sync_into(db, served)

    replaced = (
        '[[llms]]\nname="p2"\nbase_url="https://y/v1"\nmodel="m"\napi_key_ref="ROUNDTRIP_KEY"\n'
        '[[llms]]\nname="p3"\nbase_url="https://z/v1"\nmodel="m"\napi_key_ref="ROUNDTRIP_KEY"\n'
    )
    report = await _sync_into(db, served, replaced)
    assert (report.added, report.removed, report.kept) == (("p3",), ("p1",), ())

    async with AsyncBroker(db, secrets=DictSecrets({"ROUNDTRIP_KEY": "sk-test"})) as broker:
        assert await broker.count() == 2
        assert "p1" not in broker._pool

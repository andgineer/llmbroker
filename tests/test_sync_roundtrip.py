"""The documented DB-init workflow end to end: the deploy job syncs a vendored
config into the DB registry from the host's own broker, then a broker opened on
that DB alone routes a call.

The sync half and the serving half run as two separate brokers, exactly as a
release-phase job and the app process would.
"""

from unittest.mock import AsyncMock, patch

from llmbroker.broker.broker import AsyncBroker
from llmbroker.standalone.secrets import DictSecrets

_PATCH = "llmbroker.broker.router.call_provider"

_PRESET = (
    '[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="ROUNDTRIP_KEY"\n'
    '[[llms]]\nname="p2"\nbase_url="https://y/v1"\nmodel="m"\napi_key_ref="ROUNDTRIP_KEY"\n'
)


def _preset(tmp_path, body: str = _PRESET):
    f = tmp_path / "preset.toml"
    f.write_text(body)
    return str(f)


async def _sync_into(db: str, preset: str):
    """The deploy-job half: build a broker, sync, close — never `async with`, which
    would provision the still-empty registry first."""
    broker = AsyncBroker(db)
    try:
        return await broker.sync(preset)
    finally:
        await broker.aclose()


async def test_sync_then_broker_on_the_db_answers(tmp_path, monkeypatch):
    db = str(tmp_path / "broker.db")
    monkeypatch.setenv("ROUNDTRIP_KEY", "sk-test")

    report = await _sync_into(db, _preset(tmp_path))
    assert report.added == ("p1", "p2")

    async with AsyncBroker(db) as broker:
        assert await broker.count() == 2
        with patch(_PATCH, new=AsyncMock(return_value=("ok", None, None))):
            result = await broker.ask("hi")
    assert result.text == "ok"
    assert result.llm_name == "p1"


async def test_sync_bootstraps_the_key_into_the_db_secrets(tmp_path, monkeypatch):
    """The sync persists an env-resolvable key, so a later process needs no env var."""
    db = str(tmp_path / "broker.db")
    monkeypatch.setenv("ROUNDTRIP_KEY", "sk-persisted")
    await _sync_into(db, _preset(tmp_path))
    monkeypatch.delenv("ROUNDTRIP_KEY")

    async with AsyncBroker(db) as broker:
        assert broker._pool.resolved_key("p1") == "sk-persisted"


async def test_a_shrunk_lineup_keeps_the_dropped_entry(tmp_path, monkeypatch):
    """Nothing arrived to pay for the removal, so the pool does not shrink — the
    entry stays callable and the report names it on this run and every later one."""
    db = str(tmp_path / "broker.db")
    monkeypatch.setenv("ROUNDTRIP_KEY", "sk-test")
    await _sync_into(db, _preset(tmp_path))

    shrunk = (
        '[[llms]]\nname="p2"\nbase_url="https://y/v1"\nmodel="m"\napi_key_ref="ROUNDTRIP_KEY"\n'
    )
    report = await _sync_into(db, _preset(tmp_path, shrunk))
    assert report.kept == ("p1",)
    assert report.removed == ()

    async with AsyncBroker(db, secrets=DictSecrets({"ROUNDTRIP_KEY": "sk-test"})) as broker:
        assert await broker.count() == 2
        assert "p1" in broker._pool


async def test_a_replacement_carrying_the_same_ref_removes_the_old_entry(tmp_path, monkeypatch):
    db = str(tmp_path / "broker.db")
    monkeypatch.setenv("ROUNDTRIP_KEY", "sk-test")
    await _sync_into(db, _preset(tmp_path))

    replaced = (
        '[[llms]]\nname="p2"\nbase_url="https://y/v1"\nmodel="m"\napi_key_ref="ROUNDTRIP_KEY"\n'
        '[[llms]]\nname="p3"\nbase_url="https://z/v1"\nmodel="m"\napi_key_ref="ROUNDTRIP_KEY"\n'
    )
    report = await _sync_into(db, _preset(tmp_path, replaced))
    assert (report.added, report.removed, report.kept) == (("p3",), ("p1",), ())

    async with AsyncBroker(db, secrets=DictSecrets({"ROUNDTRIP_KEY": "sk-test"})) as broker:
        assert await broker.count() == 2
        assert "p1" not in broker._pool

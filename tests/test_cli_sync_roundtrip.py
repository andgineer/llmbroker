"""The documented DB-init workflow end to end: `llmbroker sync preset.toml db`,
then a broker opened on that DB alone routes a call.

The tests are synchronous because `main(["sync", ...])` owns its own
``asyncio.run`` — the broker half runs in a second, separate loop, exactly as a
host would run it after the deploy-time CLI step.
"""

import asyncio
from unittest.mock import AsyncMock, patch

from llmbroker.broker.broker import AsyncBroker
from llmbroker.cli import main
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


def test_cli_sync_then_broker_on_the_db_answers(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "broker.db")
    monkeypatch.setenv("ROUNDTRIP_KEY", "sk-test")

    assert main(["sync", _preset(tmp_path), db]) == 0
    assert "synced" in capsys.readouterr().out

    async def run():
        async with AsyncBroker(db) as broker:
            assert await broker.count() == 2
            with patch(_PATCH, new=AsyncMock(return_value=("ok", None, None))):
                result = await broker.ask("hi")
        assert result.text == "ok"
        assert result.llm_name == "p1"

    asyncio.run(run())


def test_cli_sync_bootstraps_the_key_into_the_db_secrets(tmp_path, monkeypatch):
    """`sync` persists an env-resolvable key, so a later process needs no env var."""
    db = str(tmp_path / "broker.db")
    monkeypatch.setenv("ROUNDTRIP_KEY", "sk-persisted")
    assert main(["sync", _preset(tmp_path), db]) == 0
    monkeypatch.delenv("ROUNDTRIP_KEY")

    async def run():
        async with AsyncBroker(db) as broker:
            assert broker._pool.resolved_key("p1") == "sk-persisted"

    asyncio.run(run())


def test_cli_sync_is_a_total_mirror_on_rerun(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "broker.db")
    monkeypatch.setenv("ROUNDTRIP_KEY", "sk-test")
    assert main(["sync", _preset(tmp_path), db]) == 0

    shrunk = (
        '[[llms]]\nname="p2"\nbase_url="https://y/v1"\nmodel="m"\napi_key_ref="ROUNDTRIP_KEY"\n'
    )
    assert main(["sync", _preset(tmp_path, shrunk), db]) == 0
    capsys.readouterr()

    async def run():
        async with AsyncBroker(db, secrets=DictSecrets({"ROUNDTRIP_KEY": "sk-test"})) as broker:
            assert await broker.count() == 1
            assert "p1" not in broker._pool

    asyncio.run(run())


def test_cli_sync_missing_preset_file_errors(tmp_path, capsys):
    rc = main(["sync", str(tmp_path / "absent.toml"), str(tmp_path / "b.db")])
    assert rc == 1
    assert "no such file" in capsys.readouterr().err

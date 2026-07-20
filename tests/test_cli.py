"""Unit tests for the CLI (python -m llmbroker)."""

import asyncio
import socket
import tomllib
import urllib.error
from unittest.mock import AsyncMock, MagicMock, patch

from llmbroker.broker.broker import AsyncBroker
from llmbroker.cli import main
from llmbroker.sqlite import Store as SqliteStore


def _write_toml(tmp_path, entries):
    lines = []
    for name, ref in entries:
        lines += [
            "[[llms]]",
            f'name="{name}"',
            'base_url="https://x/v1"',
            'model="m"',
            f'api_key_ref="{ref}"',
        ]
    f = tmp_path / "llms.toml"
    f.write_text("\n".join(lines) + "\n")
    return str(f)


def test_env_prints_refs(tmp_path, capsys):
    path = _write_toml(tmp_path, [("a", "KEY_A"), ("b", "KEY_B")])
    rc = main(["env", path])
    out = capsys.readouterr().out
    assert rc == 0
    assert "KEY_A=" in out
    assert "KEY_B=" in out


def test_env_deduplicates_refs(tmp_path, capsys):
    path = _write_toml(tmp_path, [("a", "K"), ("b", "K")])
    rc = main(["env", path])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("K=") == 1


def test_env_missing_file_returns_1(tmp_path, capsys):
    rc = main(["env", str(tmp_path / "nope.toml")])
    assert rc == 1
    assert "error" in capsys.readouterr().err


def test_env_prints_key_help_as_named_comment(tmp_path, capsys):
    f = tmp_path / "llms.toml"
    f.write_text(
        '[[llms]]\nname="a"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="KEY_A"\n'
        '[keys]\nKEY_A="Get it at https://example.com/keys"\n'
    )
    rc = main(["env", str(f)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "# KEY_A — Get it at https://example.com/keys" in out
    assert "KEY_A=" in out


def test_env_without_key_help_has_no_comments(tmp_path, capsys):
    path = _write_toml(tmp_path, [("a", "KEY_A")])
    rc = main(["env", path])
    out = capsys.readouterr().out
    assert rc == 0
    assert "#" not in out
    assert "KEY_A=" in out


def test_env_includes_custom_entries(tmp_path, capsys):
    f = tmp_path / "llms.toml"
    f.write_text(
        '[[llms]]\nname="pool"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="POOL_KEY"\n'
        '[[custom]]\nname="frontier"\nbase_url="https://y/v1"\nmodel="big"'
        '\napi_key_ref="PAID_KEY"\npool=false\n'
        '[keys.PAID_KEY]\nhelp = "paid key help"\n'
    )
    rc = main(["env", str(f)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "POOL_KEY=" in out
    assert "PAID_KEY=" in out  # the [[custom]] entry's key is emitted too
    assert "paid key help" in out  # its help comes from the shared [keys] table


# --- preset command ---

_FAKE_TOML = b'[[llms]]\nname="x"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n'


def _mock_urlopen(content: bytes):
    resp = MagicMock()
    resp.read.return_value = content
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock()
    return resp


def test_preset_prints_to_stdout(capsys):
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_FAKE_TOML)):
        rc = main(["preset", "freetier"])
    assert rc == 0
    assert capsys.readouterr().out == _FAKE_TOML.decode()


def test_preset_not_found_returns_1(capsys):
    exc = urllib.error.HTTPError(url="u", code=404, msg="Not Found", hdrs=None, fp=None)
    with patch("urllib.request.urlopen", side_effect=exc):
        rc = main(["preset", "nosuchpreset"])
    assert rc == 1
    assert "not found" in capsys.readouterr().err


def test_preset_http_error_returns_1(capsys):
    exc = urllib.error.HTTPError(url="u", code=500, msg="Server Error", hdrs=None, fp=None)
    with patch("urllib.request.urlopen", side_effect=exc):
        rc = main(["preset", "freetier"])
    assert rc == 1
    assert "HTTP 500" in capsys.readouterr().err


def test_preset_url_error_returns_1(capsys):
    exc = urllib.error.URLError(reason="Name or service not known")
    with patch("urllib.request.urlopen", side_effect=exc):
        rc = main(["preset", "freetier"])
    assert rc == 1
    assert "Name or service not known" in capsys.readouterr().err


def test_preset_invalid_name_returns_1(capsys):
    rc = main(["preset", "../secrets"])
    assert rc == 1
    assert "invalid preset name" in capsys.readouterr().err


# --- preset --merge ---

_FRESH_PRESET = (
    b'[[llms]]\nname="groq-new"\nbase_url="https://groq/v1"\nmodel="new"\napi_key_ref="GROQ_API_KEY"\n'
    b'\n[keys.GROQ_API_KEY]\nhelp = "groq help"\n'
)


def test_preset_merge_refreshes_llms_and_keeps_custom(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text(
        '[[llms]]\nname="groq-old"\nbase_url="https://old/v1"\nmodel="old"\napi_key_ref="GROQ_API_KEY"\n'
        '[[custom]]\nname="mine"\nbase_url="https://mine/v1"\nmodel="big"\napi_key_ref="MY_KEY"\npool=false\n'
        '[keys.MY_KEY]\nhelp = "my custom key"\n'
    )
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_FRESH_PRESET)):
        rc = main(["preset", "freetier", "--merge", str(f)])
    assert rc == 0
    data = tomllib.loads(f.read_text())
    assert [e["name"] for e in data["llms"]] == ["groq-new"]  # old managed entry gone
    assert [e["name"] for e in data["custom"]] == ["mine"]  # custom preserved
    assert data["custom"][0]["pool"] is False
    assert data["keys"]["GROQ_API_KEY"]["help"] == "groq help"  # refreshed from preset
    assert data["keys"]["MY_KEY"]["help"] == "my custom key"  # custom key preserved


def test_preset_merge_creates_file_when_absent(tmp_path):
    f = tmp_path / "new.toml"
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_FAKE_TOML)):
        rc = main(["preset", "freetier", "--merge", str(f)])
    assert rc == 0
    data = tomllib.loads(f.read_text())
    assert [e["name"] for e in data["llms"]] == ["x"]
    assert "custom" not in data


def test_preset_merge_rejects_non_toml(tmp_path, capsys):
    f = tmp_path / "llms.json"
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_FAKE_TOML)):
        rc = main(["preset", "freetier", "--merge", str(f)])
    assert rc == 1
    assert ".toml" in capsys.readouterr().err


def test_preset_invalid_toml_returns_1(capsys):
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(b"not toml ]][")):
        rc = main(["preset", "freetier"])
    assert rc == 1
    assert "not valid TOML" in capsys.readouterr().err


def test_preset_invalid_encoding_returns_1(capsys):
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(b"\xff\xfe bad")):
        rc = main(["preset", "freetier"])
    assert rc == 1
    assert "UTF-8" in capsys.readouterr().err


def test_preset_timeout_returns_1(capsys):
    exc = urllib.error.URLError(reason=socket.timeout("timed out"))
    with patch("urllib.request.urlopen", side_effect=exc):
        rc = main(["preset", "freetier"])
    assert rc == 1
    assert "timed out" in capsys.readouterr().err


# --- sync command ---


def test_sync_seeds_disabled_map_on_the_target_db_via_source_dispatch(tmp_path, monkeypatch):
    """sync must dispatch on the db argument (source dispatch), so `llmbroker_disabled`
    is seeded on the target DB — not on a stray `./store` sibling under the CWD."""
    monkeypatch.chdir(tmp_path)
    preset = _write_toml(tmp_path, [("a", "KEY_A"), ("b", "KEY_B")])
    db = str(tmp_path / "x.db")

    rc = main(["sync", preset, db])
    assert rc == 0

    disabled = asyncio.run(SqliteStore(db).disabled_map())
    assert disabled == {"a": False, "b": False}
    assert not (tmp_path / "store").exists()


def test_sync_missing_file_returns_1(tmp_path, capsys):
    rc = main(["sync", str(tmp_path / "nope.toml"), str(tmp_path / "x.db")])
    assert rc == 1
    assert "error" in capsys.readouterr().err


# --- env -> sync -> ask round trip (mission #2) ---


def test_env_sync_ask_round_trip(tmp_path, capsys, monkeypatch):
    """A preset's api_key_ref surfaces through `env`, `sync` mirrors it into a DB
    registry, and a broker built over that DB routes a real call."""
    f = tmp_path / "preset.toml"
    f.write_text(
        '[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="KEY_A"\n'
        '[keys.KEY_A]\nhelp="Get it at https://example.com/keys"\n'
    )
    preset = str(f)
    db = str(tmp_path / "x.db")

    rc = main(["env", preset])
    out = capsys.readouterr().out
    assert rc == 0
    assert "KEY_A" in out
    assert "Get it at https://example.com/keys" in out

    monkeypatch.setenv("KEY_A", "test-key")

    rc = main(["sync", preset, db])
    assert rc == 0

    async def run():
        async with AsyncBroker(db) as broker:
            with patch(
                "llmbroker.broker.router.call_provider",
                new=AsyncMock(return_value=("hi", None, None)),
            ):
                result = await broker.ask("hello")
        assert result.text == "hi"

    asyncio.run(run())

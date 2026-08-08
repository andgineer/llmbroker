"""Unit tests for the CLI (python -m llmbroker)."""

import asyncio
import http.client
import socket
import tomllib
import urllib.error
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llmbroker.broker.broker import AsyncBroker
from llmbroker.cli import main
from llmbroker.models import Call, CallStatus
from llmbroker.sqlite import Store as SqliteStore
from llmbroker.standalone.store import FileStore


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
    # A bare MagicMock __exit__ returns a truthy mock, which swallows whatever the
    # body raises inside the `with`.
    resp.__exit__ = lambda *_a: False
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
        rc = main(["preset", "notbundled"])
    assert rc == 1
    assert "HTTP 500" in capsys.readouterr().err


def test_preset_url_error_returns_1(capsys):
    exc = urllib.error.URLError(reason="Name or service not known")
    with patch("urllib.request.urlopen", side_effect=exc):
        rc = main(["preset", "notbundled"])
    assert rc == 1
    assert "Name or service not known" in capsys.readouterr().err


def test_preset_invalid_name_returns_1(capsys):
    rc = main(["preset", "../secrets"])
    assert rc == 1
    assert "invalid preset name" in capsys.readouterr().err


def test_preset_body_dying_mid_read_returns_1(capsys):
    """A read that fails after the connect phase used to traceback out of the CLI."""
    resp = _mock_urlopen(b"")
    resp.read.side_effect = http.client.IncompleteRead(b"partial")
    with patch("urllib.request.urlopen", return_value=resp):
        rc = main(["preset", "notbundled"])
    assert rc == 1
    assert "failed reading" in capsys.readouterr().err


def test_preset_sync_into_a_missing_directory_returns_1(tmp_path, capsys):
    """The writer's own OSError is an error message, not a traceback."""
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_FAKE_TOML)):
        rc = main(["preset", "freetier", "--sync", str(tmp_path / "nodir" / "llms.toml")])
    assert rc == 1
    assert capsys.readouterr().err.startswith("error: ")


# --- preset --sync ---

_FRESH_PRESET = (
    b'[[llms]]\nname="groq-new"\nbase_url="https://groq/v1"\nmodel="new"\napi_key_ref="GROQ_API_KEY"\n'
    b'\n[keys.GROQ_API_KEY]\nhelp = "groq help"\n'
)


def test_preset_sync_refreshes_llms_and_keeps_custom(tmp_path):
    """An entry the host left in llmbroker's own file is carried over — re-rendered
    from its fields, so a key llmbroker does not model goes with the rewrite."""
    f = tmp_path / "llms.toml"
    f.write_text(
        '[[llms]]\nname="groq-old"\nbase_url="https://old/v1"\nmodel="old"\napi_key_ref="GROQ_API_KEY"\n'
        '[[custom]]\nname="mine"\nbase_url="https://mine/v1"\nmodel="big"\napi_key_ref="MY_KEY"\npool=false\n'
        '[keys.MY_KEY]\nhelp = "my custom key"\n'
    )
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_FRESH_PRESET)):
        rc = main(["preset", "freetier", "--sync", str(f)])
    assert rc == 0
    data = tomllib.loads(f.read_text())
    assert [e["name"] for e in data["llms"]] == ["groq-new"]  # old managed entry gone
    assert [e["name"] for e in data["custom"]] == ["mine"]  # custom preserved
    assert "pool" not in data["custom"][0]
    assert data["keys"]["GROQ_API_KEY"]["help"] == "groq help"  # refreshed from preset
    assert data["keys"]["MY_KEY"]["help"] == "my custom key"  # custom key preserved


def test_a_name_a_user_model_uses_is_refused(tmp_path):
    """One file, one lineup: a name already taken would stop it loading at the next
    start, so the sync refuses instead of writing it."""
    f = tmp_path / "llms.toml"
    f.write_text(
        '[[custom]]\nname="groq-new"\nbase_url="https://mine/v1"\nmodel="big"\napi_key_ref="MY_KEY"\n'
    )
    before = f.read_text()
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_FRESH_PRESET)):
        rc = main(["preset", "freetier", "--sync", str(f)])
    assert rc == 1
    assert f.read_text() == before


def test_the_generated_file_says_it_is_generated(tmp_path):
    """It is rewritten in full, so a reader opening it has to be told not to edit it."""
    f = tmp_path / "llms.toml"
    f.write_text("# a note of my own\n")
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_FRESH_PRESET)):
        rc = main(["preset", "freetier", "--sync", str(f)])
    assert rc == 0
    text = f.read_text()
    assert "a note of my own" not in text
    assert text.startswith("# Generated by llmbroker")
    assert "add-model" in text


def test_preset_sync_creates_file_when_absent(tmp_path):
    f = tmp_path / "new.toml"
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_FAKE_TOML)):
        rc = main(["preset", "freetier", "--sync", str(f)])
    assert rc == 0
    data = tomllib.loads(f.read_text())
    assert [e["name"] for e in data["llms"]] == ["x"]
    assert "custom" not in data


def test_preset_sync_keeps_a_short_custom_entry_out_of_the_keys_table(tmp_path):
    """A custom entry short enough for tomli_w to render inline must still land as a
    top-level [[custom]] entry, not inside the preset's trailing [keys.*] table."""
    f = tmp_path / "llms.toml"
    f.write_text('[[custom]]\nname="l"\nbase_url="http://h/v1"\nmodel="m"\napi_key_ref="K"\n')
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_FRESH_PRESET)):
        rc = main(["preset", "freetier", "--sync", str(f)])
    assert rc == 0
    data = tomllib.loads(f.read_text())
    assert [e["name"] for e in data["custom"]] == ["l"]
    assert "custom" not in data["keys"]["GROQ_API_KEY"]


_REFRESH_CATALOG = (
    b'[[provider]]\nid="anthropic"\nlabel="Anthropic"\n'
    b'base_url="https://api.anthropic.com/v2"\napi_key_ref="ANTHROPIC_API_KEY"\n'
    b'key_help="console.anthropic.com"\n'
    b'  [[provider.models]]\n  alias="opus"\n  model="claude-opus-5"\n'
    b'  label="Opus"\n  verified="u"\n'
)


def _mock_fetch(*bodies: bytes):
    """urlopen side_effect serving each body in turn (preset first, then catalog)."""
    return [_mock_urlopen(b) for b in bodies]


def _alias_file(tmp_path, extra: str = "") -> object:
    f = tmp_path / "llms.toml"
    f.write_text(
        '[[llms]]\nname="groq-old"\nbase_url="https://old/v1"\nmodel="old"'
        '\napi_key_ref="GROQ_API_KEY"\n'
        '[[custom]]\nalias="opus"\nname="anthropic-claude-opus-4-8"'
        '\nmodel="claude-opus-4-8"\nbase_url="https://api.anthropic.com/v1"'
        '\napi_key_ref="ANTHROPIC_API_KEY"\npool=false\n' + extra
    )
    return f


def test_preset_sync_refreshes_alias_entry(tmp_path, capsys):
    f = _alias_file(tmp_path)
    with patch(
        "urllib.request.urlopen",
        side_effect=_mock_fetch(_FRESH_PRESET, _REFRESH_CATALOG),
    ):
        rc = main(["preset", "freetier", "--sync", str(f)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "opus: claude-opus-4-8 -> claude-opus-5" in out
    (entry,) = tomllib.loads(f.read_text())["custom"]
    assert entry["model"] == "claude-opus-5"
    assert entry["name"] == "anthropic-claude-opus-5"  # name follows the version
    assert entry["base_url"] == "https://api.anthropic.com/v2"
    assert entry["alias"] == "opus"


def test_preset_sync_leaves_a_pinned_entry_byte_identical(tmp_path):
    """A pin is the host's entirely, and it lives in the file a sync never opens for
    writing — so "untouched" is a property of the bytes, not of a comparison."""
    f = _alias_file(tmp_path)
    host = tmp_path / "custom.toml"
    host.write_text(
        '[[custom]]\nname="frontier"\nmodel="claude-opus-4-8"'
        '\nbase_url="https://api.anthropic.com/v1"\napi_key_ref="ANTHROPIC_API_KEY"\npool=false\n'
    )
    before = host.read_bytes()
    with patch(
        "urllib.request.urlopen",
        side_effect=_mock_fetch(_FRESH_PRESET, _REFRESH_CATALOG),
    ):
        rc = main(["preset", "freetier", "--sync", str(f)])
    assert rc == 0
    assert host.read_bytes() == before
    # and the alias entry beside it did move
    (entry,) = tomllib.loads(f.read_text())["custom"]
    assert entry["name"] == "anthropic-claude-opus-5"


def test_preset_sync_unknown_alias_warns_and_keeps_entry(tmp_path, capsys):
    f = tmp_path / "llms.toml"
    f.write_text(
        '[[custom]]\nalias="ghost"\nname="x-y"\nmodel="y"\nbase_url="https://x/v1"'
        '\napi_key_ref="X_KEY"\npool=false\n'
    )
    with patch(
        "urllib.request.urlopen",
        side_effect=_mock_fetch(_FRESH_PRESET, _REFRESH_CATALOG),
    ):
        rc = main(["preset", "freetier", "--sync", str(f)])
    assert rc == 0
    assert "alias 'ghost' is not in the paid catalog" in capsys.readouterr().err
    (entry,) = tomllib.loads(f.read_text())["custom"]
    assert entry["model"] == "y"


def test_preset_sync_without_alias_entries_fetches_no_catalog(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text(
        '[[custom]]\nname="mine"\nbase_url="https://mine/v1"\nmodel="big"'
        '\napi_key_ref="MY_KEY"\npool=false\n'
    )
    fetches: list[str] = []

    def urlopen(url, *_a, **_kw):
        fetches.append(url)
        return _mock_urlopen(_FRESH_PRESET)

    with patch("urllib.request.urlopen", side_effect=urlopen):
        rc = main(["preset", "freetier", "--sync", str(f)])
    assert rc == 0
    assert len(fetches) == 1
    assert "paid-catalog" not in fetches[0]


def test_preset_sync_new_api_key_ref_brings_its_keys_help(tmp_path):
    """A refresh that moves an alias onto another provider carries the new ref's help."""
    catalog = (
        b'[[provider]]\nid="other"\nlabel="Other"\nbase_url="https://other/v1"\n'
        b'api_key_ref="OTHER_API_KEY"\nkey_help="get a key at other.example"\n'
        b'  [[provider.models]]\n  alias="opus"\n  model="other-big"\n'
        b'  label="Big"\n  verified="u"\n'
    )
    f = _alias_file(tmp_path)
    with patch("urllib.request.urlopen", side_effect=_mock_fetch(_FRESH_PRESET, catalog)):
        rc = main(["preset", "freetier", "--sync", str(f)])
    assert rc == 0
    data = tomllib.loads(f.read_text())
    assert data["custom"][0]["api_key_ref"] == "OTHER_API_KEY"
    assert data["keys"]["OTHER_API_KEY"]["help"] == "get a key at other.example"


def test_preset_sync_reports_a_changed_api_key_ref(tmp_path, capsys):
    """A catalog that only re-spells a provider's ref moves nothing else, so there
    is no model line to notice — and the file quietly starts wanting a new env var."""
    catalog = (
        b'[[provider]]\nid="anthropic"\nlabel="Anthropic"\n'
        b'base_url="https://api.anthropic.com/v1"\napi_key_ref="CLAUDE_API_KEY"\n'
        b'  [[provider.models]]\n  alias="opus"\n  model="claude-opus-4-8"\n'
        b'  label="Opus"\n  verified="u"\n'
    )
    f = _alias_file(tmp_path)
    with patch("urllib.request.urlopen", side_effect=_mock_fetch(_FRESH_PRESET, catalog)):
        rc = main(["preset", "freetier", "--sync", str(f)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "opus: api_key_ref ANTHROPIC_API_KEY -> CLAUDE_API_KEY" in out
    assert "set CLAUDE_API_KEY before the next call" in out
    (entry,) = tomllib.loads(f.read_text())["custom"]
    assert (entry["model"], entry["name"]) == ("claude-opus-4-8", "anthropic-claude-opus-4-8")


def test_preset_sync_duplicate_catalog_alias_is_an_error(tmp_path, capsys):
    catalog = _REFRESH_CATALOG + (
        b'[[provider]]\nid="dup"\nlabel="Dup"\nbase_url="https://dup/v1"\napi_key_ref="D_KEY"\n'
        b'  [[provider.models]]\n  alias="opus"\n  model="dup-1"\n  label="D"\n  verified="u"\n'
    )
    f = _alias_file(tmp_path)
    original = f.read_text()
    with patch("urllib.request.urlopen", side_effect=_mock_fetch(_FRESH_PRESET, catalog)):
        rc = main(["preset", "freetier", "--sync", str(f)])
    assert rc == 1
    assert "alias 'opus' is used twice" in capsys.readouterr().err
    assert f.read_text() == original  # nothing written


def test_preset_sync_refuses_to_rename_an_alias_onto_a_pool_entry(tmp_path, capsys):
    """The refreshed name lands on a preset [[llms]] name: writing that file would
    lose one of the two entries at the next sync, so nothing is written."""
    preset = (
        b'[[llms]]\nname="anthropic-claude-opus-5"\nbase_url="https://free/v1"\n'
        b'model="claude-opus-5"\napi_key_ref="FREE_KEY"\n'
    )
    f = _alias_file(tmp_path)
    original = f.read_text()
    with patch("urllib.request.urlopen", side_effect=_mock_fetch(preset, _REFRESH_CATALOG)):
        rc = main(["preset", "freetier", "--sync", str(f)])
    assert rc == 1
    assert "two entries named 'anthropic-claude-opus-5'" in capsys.readouterr().err
    assert f.read_text() == original


def test_preset_sync_rejects_non_toml(tmp_path, capsys):
    f = tmp_path / "llms.json"
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_FAKE_TOML)):
        rc = main(["preset", "freetier", "--sync", str(f)])
    assert rc == 1
    assert ".toml" in capsys.readouterr().err


# --- add-model command ---

_CATALOG = (
    b'[[provider]]\nid="anthropic"\nlabel="Anthropic"\n'
    b'base_url="https://api.anthropic.com/v1"\napi_key_ref="ANTHROPIC_API_KEY"\n'
    b'key_help="console.anthropic.com"\n'
    b'  [[provider.models]]\n  alias="opus"\n  model="claude-opus-4-8"\n'
    b'  label="Opus"\n  verified="u"\n'
    b'  [[provider.models]]\n  alias="sonnet"\n  model="claude-sonnet-5"\n'
    b'  label="Sonnet"\n  verified="u"\n'
)


def _lineup(
    home, body='[[llms]]\nname="pool"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="POOL_KEY"\n'
):
    """`add-model` writes the lineup inside llmbroker's own directory, which the
    autouse fixture points at a temp dir."""
    f = home / "lineup.toml"
    f.write_text(body)
    return f


def test_add_model_flags_appends_alias_custom(llmbroker_home):
    f = _lineup(llmbroker_home)
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_CATALOG)):
        rc = main(["add-model", "--provider", "anthropic", "--model", "claude-opus-4-8"])
    assert rc == 0
    data = tomllib.loads(f.read_text())
    assert [e["name"] for e in data["llms"]] == ["pool"]  # existing entry preserved
    (entry,) = data["custom"]
    assert entry["alias"] == "opus"
    assert entry["name"] == "anthropic-claude-opus-4-8"  # machine-formed
    assert entry["model"] == "claude-opus-4-8"
    assert entry["base_url"] == "https://api.anthropic.com/v1"
    assert entry["api_key_ref"] == "ANTHROPIC_API_KEY"
    assert "pool" not in entry  # a custom entry is direct-only by being custom
    assert data["keys"]["ANTHROPIC_API_KEY"]["help"] == "console.anthropic.com"


def test_add_model_pin_writes_name_only_block(llmbroker_home):
    f = _lineup(llmbroker_home)
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_CATALOG)):
        rc = main(
            [
                "add-model",
                "--provider",
                "anthropic",
                "--model",
                "claude-sonnet-5",
                "--pin",
                "--name",
                "frontier",
            ]
        )
    assert rc == 0
    (entry,) = tomllib.loads(f.read_text())["custom"]
    assert "alias" not in entry
    assert entry["name"] == "frontier"
    assert entry["model"] == "claude-sonnet-5"


def test_add_model_pin_defaults_name_to_provider_id(llmbroker_home):
    f = _lineup(llmbroker_home)
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_CATALOG)):
        rc = main(
            [
                "add-model",
                "--provider",
                "anthropic",
                "--model",
                "claude-opus-4-8",
                "--pin",
            ]
        )
    assert rc == 0
    (entry,) = tomllib.loads(f.read_text())["custom"]
    assert entry["name"] == "anthropic"
    assert "alias" not in entry


def test_add_model_name_without_pin_errors(llmbroker_home, capsys):
    f = _lineup(llmbroker_home)
    rc = main(
        [
            "add-model",
            "--provider",
            "anthropic",
            "--model",
            "claude-opus-4-8",
            "--name",
            "mine",
        ]
    )
    assert rc == 1
    assert "--name is only valid with --pin" in capsys.readouterr().err


def test_add_model_alias_collision_refused(llmbroker_home, capsys):
    _lineup(
        llmbroker_home,
        '[[custom]]\nalias="opus"\nname="something-else"\nbase_url="https://x/v1"\n'
        'model="old"\napi_key_ref="ANTHROPIC_API_KEY"\n',
    )
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_CATALOG)):
        rc = main(["add-model", "--provider", "anthropic", "--model", "claude-opus-4-8"])
    assert rc == 1
    assert "alias 'opus' is already used" in capsys.readouterr().err


def test_add_model_catalog_model_without_alias_needs_pin(llmbroker_home, capsys):
    catalog = (
        b'[[provider]]\nid="x"\nlabel="X"\nbase_url="https://x/v1"\napi_key_ref="X_KEY"\n'
        b'  [[provider.models]]\n  model="m1"\n  label="M1"\n  verified="u"\n'
    )
    f = _lineup(llmbroker_home)
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(catalog)):
        rc = main(["add-model", "--provider", "x", "--model", "m1"])
    assert rc == 1
    assert "carries no alias" in capsys.readouterr().err


def test_add_model_unknown_provider(llmbroker_home, capsys):
    f = _lineup(llmbroker_home)
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_CATALOG)):
        rc = main(["add-model", "--provider", "nope", "--model", "x"])
    assert rc == 1
    assert "unknown provider" in capsys.readouterr().err


def test_add_model_unknown_model(llmbroker_home, capsys):
    f = _lineup(llmbroker_home)
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_CATALOG)):
        rc = main(["add-model", "--provider", "anthropic", "--model", "ghost"])
    assert rc == 1
    assert "unknown model" in capsys.readouterr().err


def test_add_model_name_collision(llmbroker_home, capsys):
    f = _lineup(llmbroker_home)
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_CATALOG)):
        rc = main(
            [
                "add-model",
                "--provider",
                "anthropic",
                "--model",
                "claude-opus-4-8",
                "--pin",
                "--name",
                "pool",
            ]  # collides with the [[llms]] entry
        )
    assert rc == 1
    assert "already exists" in capsys.readouterr().err


def test_add_model_does_not_duplicate_existing_key(llmbroker_home):
    f = _lineup(llmbroker_home, '[keys.ANTHROPIC_API_KEY]\nhelp = "existing help"\n')
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_CATALOG)):
        rc = main(["add-model", "--provider", "anthropic", "--model", "claude-opus-4-8"])
    assert rc == 0
    data = tomllib.loads(f.read_text())  # still valid TOML, key kept once
    assert data["keys"]["ANTHROPIC_API_KEY"]["help"] == "existing help"


_SHORT_CATALOG = (
    b'[[provider]]\nid="h"\nlabel="H"\nbase_url="https://h"\napi_key_ref="K"\n'
    b'key_help="hh"\n  [[provider.models]]\n  alias="m"\n  model="m"\n'
    b'  label="M"\n  verified="u"\n'
)


def test_add_model_keeps_a_short_entry_out_of_a_trailing_keys_table(llmbroker_home):
    """A catalog entry short enough for tomli_w to render inline must still land as a
    top-level [[custom]] entry, not inside the file's trailing [keys.*] table."""
    f = _lineup(llmbroker_home, '[keys.POOL_KEY]\nhelp = "pool help"\n')
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_SHORT_CATALOG)):
        rc = main(
            [
                "add-model",
                "--provider",
                "h",
                "--model",
                "m",
                "--pin",
                "--name",
                "x",
            ]
        )
    assert rc == 0
    data = tomllib.loads(f.read_text())
    assert [e["name"] for e in data["custom"]] == ["x"]
    assert "custom" not in data["keys"]["POOL_KEY"]
    assert data["keys"]["K"]["help"] == "hh"


def test_add_model_interactive(llmbroker_home, capsys):
    f = _lineup(llmbroker_home)
    with (
        patch("urllib.request.urlopen", return_value=_mock_urlopen(_CATALOG)),
        patch("builtins.input", side_effect=["1", "2"]),  # provider, model
    ):
        rc = main(["add-model"])
    assert rc == 0
    assert "sonnet — Sonnet (claude-sonnet-5)" in capsys.readouterr().out  # alias-led menu
    (entry,) = tomllib.loads(f.read_text())["custom"]
    assert entry["alias"] == "sonnet"  # picked #2
    assert entry["name"] == "anthropic-claude-sonnet-5"
    assert entry["model"] == "claude-sonnet-5"


def test_add_model_offline_reads_the_bundled_catalog(llmbroker_home, capsys, bundled_presets):
    """The catalog ships in the wheel, so picking a paid model needs no network."""
    f = _lineup(llmbroker_home)
    exc = urllib.error.URLError(reason="offline")
    with patch("urllib.request.urlopen", side_effect=exc):
        rc = main(["add-model", "--provider", "anthropic", "--model", "opus"])
    assert rc == 1  # the model id is wrong, but the catalog was read
    assert "unknown model 'opus'" in capsys.readouterr().err


def test_add_model_reports_a_fetch_failure_with_nothing_to_fall_back_to(llmbroker_home, capsys):
    f = _lineup(llmbroker_home)
    exc = urllib.error.URLError(reason="offline")
    with patch("urllib.request.urlopen", side_effect=exc):
        rc = main(["add-model", "--provider", "anthropic", "--model", "claude-opus-4-8"])
    assert rc == 1
    assert "offline" in capsys.readouterr().err


def test_preset_offline_prints_the_bundled_copy(capsys, bundled_presets):
    """`llmbroker preset` and `llmbroker env` work with no network at all."""
    exc = urllib.error.URLError(reason="offline")
    with patch("urllib.request.urlopen", side_effect=exc):
        rc = main(["preset", "freetier"])
    assert rc == 0
    assert "[[llms]]" in capsys.readouterr().out


def test_env_offline_prints_the_bundled_presets_refs(capsys, bundled_presets):
    exc = urllib.error.URLError(reason="offline")
    with patch("urllib.request.urlopen", side_effect=exc):
        rc = main(["env", "freetier"])
    assert rc == 0
    assert "GEMINI_API_KEY=" in capsys.readouterr().out


def test_add_model_incomplete_catalog_entry_errors_cleanly(llmbroker_home, capsys):
    # provider missing base_url/api_key_ref — must be a clean error, not a traceback
    bad = (
        b'[[provider]]\nid="x"\nlabel="X"\n'
        b'  [[provider.models]]\n  model="m1"\n  label="M1"\n  verified="u"\n'
    )
    f = _lineup(llmbroker_home)
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(bad)):
        rc = main(["add-model", "--provider", "x", "--model", "m1"])
    assert rc == 1
    assert "incomplete" in capsys.readouterr().err


def test_add_model_interactive_honors_the_name_flag(llmbroker_home):
    f = _lineup(llmbroker_home)
    with (
        patch("urllib.request.urlopen", return_value=_mock_urlopen(_CATALOG)),
        patch("builtins.input", side_effect=["1", "1", ""]),  # provider, model, name = default
    ):
        rc = main(["add-model", "--pin", "--name", "myclaude"])
    assert rc == 0
    (entry,) = tomllib.loads(f.read_text())["custom"]
    assert entry["name"] == "myclaude"  # --name used as the prompt default, accepted blank


def test_add_model_eof_aborts_cleanly(llmbroker_home, capsys):
    f = _lineup(llmbroker_home)
    with (
        patch("urllib.request.urlopen", return_value=_mock_urlopen(_CATALOG)),
        patch("builtins.input", side_effect=EOFError),
    ):
        rc = main(["add-model"])
    assert rc == 1
    assert "aborted" in capsys.readouterr().err


def test_preset_invalid_toml_returns_1(capsys):
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(b"not toml ]][")):
        rc = main(["preset", "notbundled"])
    assert rc == 1
    assert "not valid TOML" in capsys.readouterr().err


def test_preset_invalid_encoding_returns_1(capsys):
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(b"\xff\xfe bad")):
        rc = main(["preset", "notbundled"])
    assert rc == 1
    assert "UTF-8" in capsys.readouterr().err


def test_preset_timeout_returns_1(capsys):
    exc = urllib.error.URLError(reason=socket.timeout("timed out"))
    with patch("urllib.request.urlopen", side_effect=exc):
        rc = main(["preset", "notbundled"])
    assert rc == 1
    assert "timed out" in capsys.readouterr().err


# --- the report the CLI prints, and the keys that drive it ---

_GEMINI_PRESET = (
    b'[[llms]]\nname="gemini"\nbase_url="https://g/v1"\nmodel="m"\napi_key_ref="GEMINI_KEY"\n'
)


def _with_old_entry(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text(
        '[[llms]]\nname="groq-old"\nbase_url="https://groq/v1"\nmodel="m"\napi_key_ref="GROQ_KEY"\n'
    )
    return f


def test_sync_prints_the_report_on_a_no_op_run(tmp_path, capsys, monkeypatch):
    """Nothing changed, and it still says so — a pending key must keep nagging in
    every deploy log until someone sets it."""
    monkeypatch.delenv("K", raising=False)
    f = tmp_path / "llms.toml"
    f.write_text(_FAKE_TOML.decode())
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_FAKE_TOML)):
        rc = main(["preset", "freetier", "--sync", str(f)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "sync freetier: applied" in out
    assert "pending key K" in out


def test_sync_removes_the_dropped_entry_when_the_arrivals_key_is_set(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("GEMINI_KEY", "sk-real")
    f = _with_old_entry(tmp_path)
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_GEMINI_PRESET)):
        rc = main(["preset", "freetier", "--sync", str(f)])
    assert rc == 0
    assert "removed: groq-old" in capsys.readouterr().out
    assert [e["name"] for e in tomllib.loads(f.read_text())["llms"]] == ["gemini"]


def test_an_empty_key_makes_the_probe_blind_and_removes_nothing(tmp_path, capsys, monkeypatch):
    """A blank export is as unset as no export at all — and a probe that resolved
    nothing cannot tell 'no key anywhere' from 'not the keys this lineup runs on'."""
    monkeypatch.setenv("GEMINI_KEY", "")
    monkeypatch.delenv("GROQ_KEY", raising=False)
    f = _with_old_entry(tmp_path)
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_GEMINI_PRESET)):
        rc = main(["preset", "freetier", "--sync", str(f)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "kept: groq-old" in out
    assert "no key resolved here at all" in out
    assert [e["name"] for e in tomllib.loads(f.read_text())["llms"]] == ["gemini", "groq-old"]


def test_the_sibling_store_supplies_the_retirement_evidence(tmp_path, capsys, monkeypatch):
    """The CLI has no broker, so it opens the journal the same way one would — and
    a provider whose key is still here goes only once that journal condemns it."""
    monkeypatch.setenv("GEMINI_KEY", "sk-gem")
    monkeypatch.setenv("GROQ_KEY", "sk-groq")
    f = _with_old_entry(tmp_path)

    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_GEMINI_PRESET)):
        rc = main(["preset", "freetier", "--sync", str(f)])
    assert rc == 0
    assert "kept: groq-old" in capsys.readouterr().out

    store = FileStore(tmp_path / "store")
    asyncio.run(
        store.record(
            Call(
                id="a",
                llm_name="groq-old",
                operation=None,
                trace_id=None,
                status=CallStatus.ERROR,
                kind="call",
                ts=datetime(2030, 1, 1, tzinfo=UTC),
                http_status=401,
            ),
        ),
    )
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_GEMINI_PRESET)):
        rc = main(["preset", "freetier", "--sync", str(f)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "retired: groq-old" in out
    assert [e["name"] for e in tomllib.loads(f.read_text())["llms"]] == ["gemini"]


def test_without_a_sibling_store_nothing_is_retired(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("GEMINI_KEY", "sk-gem")
    monkeypatch.setenv("GROQ_KEY", "sk-groq")
    f = _with_old_entry(tmp_path)
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_GEMINI_PRESET)):
        rc = main(["preset", "freetier", "--sync", str(f)])
    assert rc == 0
    assert "kept: groq-old" in capsys.readouterr().out


def test_the_sibling_env_file_counts_as_a_key(tmp_path, capsys, monkeypatch):
    """The CLI resolves what the application will: env plus the config's own .env."""
    monkeypatch.delenv("GEMINI_KEY", raising=False)
    f = _with_old_entry(tmp_path)
    (tmp_path / ".env").write_text("GEMINI_KEY=sk-from-file\n")
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_GEMINI_PRESET)):
        rc = main(["preset", "freetier", "--sync", str(f)])
    assert rc == 0
    assert "removed: groq-old" in capsys.readouterr().out


# --- a DB target belongs to the application, not the CLI ---


def test_sync_into_a_db_target_is_refused_with_a_pointer(tmp_path, capsys):
    """The CLI writes files only: a DSN here would duplicate connection config the
    application already owns, and force DB credentials into the CLI's environment."""
    for target in ("broker.db", "x.sqlite", "postgresql://h/db", "mongodb://h/db"):
        with patch("urllib.request.urlopen", return_value=_mock_urlopen(_FAKE_TOML)):
            rc = main(["preset", "freetier", "--sync", target])
        assert rc == 1
        err = capsys.readouterr().err
        assert "broker.sync" in err


def test_the_sync_subcommand_is_gone(capsys):
    """Mirroring into a registry is the host's own entrypoint now."""
    with pytest.raises(SystemExit):
        main(["sync", "preset.toml", "broker.db"])
    assert "invalid choice: 'sync'" in capsys.readouterr().err


# --- env -> sync -> ask round trip (mission #2) ---


def test_env_sync_ask_round_trip(tmp_path, capsys, monkeypatch):
    """A preset's api_key_ref surfaces through `env`, the app's own entrypoint syncs
    it into a DB registry, and a broker built over that DB routes a real call."""
    monkeypatch.chdir(tmp_path)
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

    async def run():
        broker = AsyncBroker(db)
        try:
            report = await broker.sync(preset)
        finally:
            await broker.aclose()
        assert report.added == ("p1",)

        async with AsyncBroker(db) as broker:
            # seeded on the target DB (source dispatch), not a stray ./store sibling
            assert await SqliteStore(db).disabled_map() == {"p1": False}
            assert not (tmp_path / "store").exists()
            with patch(
                "llmbroker.broker.router.call_provider",
                new=AsyncMock(return_value=("hi", None, None)),
            ):
                result = await broker.ask("hello")
        assert result.text == "hi"

    asyncio.run(run())

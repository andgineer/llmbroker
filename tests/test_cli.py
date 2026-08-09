"""Unit tests for the CLI (python -m llmbroker)."""

import asyncio
import tomllib
import urllib.error
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llmbroker.broker import presets
from llmbroker.broker.broker import AsyncBroker
from llmbroker.cli import main
from llmbroker.sqlite import Store as SqliteStore


def _mock_urlopen(content: bytes):
    resp = MagicMock()
    resp.read.return_value = content
    resp.__enter__ = lambda s: s
    # A bare MagicMock __exit__ returns a truthy mock, which swallows whatever the
    # body raises inside the `with`.
    resp.__exit__ = lambda *_a: False
    return resp


# --- env: this installation's own lineup, or a curated preset by name ---


def test_env_prints_the_refs_of_this_installations_lineup(llmbroker_home, capsys):
    _lineup(
        llmbroker_home,
        '[[llms]]\nname="a"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="KEY_A"\n'
        '[[llms]]\nname="b"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="KEY_B"\n',
    )
    rc = main(["env"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "KEY_A=" in out
    assert "KEY_B=" in out


def test_env_deduplicates_refs(llmbroker_home, capsys):
    _lineup(
        llmbroker_home,
        '[[llms]]\nname="a"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n'
        '[[llms]]\nname="b"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n',
    )
    rc = main(["env"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("K=") == 1


def test_env_prints_key_help_as_named_comment(llmbroker_home, capsys):
    _lineup(
        llmbroker_home,
        '[[llms]]\nname="a"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="KEY_A"\n'
        '[keys]\nKEY_A="Get it at https://example.com/keys"\n',
    )
    rc = main(["env"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "# KEY_A — Get it at https://example.com/keys" in out
    assert "KEY_A=" in out


def test_env_without_key_help_has_no_comments(llmbroker_home, capsys):
    _lineup(
        llmbroker_home,
        '[[llms]]\nname="a"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="KEY_A"\n',
    )
    rc = main(["env"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "#" not in out
    assert "KEY_A=" in out


def test_env_includes_custom_entries(llmbroker_home, capsys):
    _lineup(
        llmbroker_home,
        '[[llms]]\nname="pool"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="POOL_KEY"\n'
        '[[custom]]\nname="frontier"\nbase_url="https://y/v1"\nmodel="big"'
        '\napi_key_ref="PAID_KEY"\n'
        '[keys.PAID_KEY]\nhelp = "paid key help"\n',
    )
    rc = main(["env"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "POOL_KEY=" in out
    assert "PAID_KEY=" in out  # the [[custom]] entry's key is emitted too
    assert "paid key help" in out  # its help comes from the shared [keys] table


def test_env_before_any_broker_has_run_says_so_instead_of_printing_nothing(
    llmbroker_home,
    capsys,
):
    """Onboarding runs this before the lineup exists. An empty skeleton with a zero
    exit code reads as "no keys needed", which is the opposite of the truth."""
    rc = main(["env"])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out == ""
    assert "no lineup yet" in captured.err
    assert "llmbroker env freetier" in captured.err


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


# --- what the CLI is not ---


@pytest.mark.parametrize("argv", [["sync", "preset.toml", "broker.db"], ["preset", "freetier"]])
def test_the_removed_subcommands_stay_removed(capsys, argv):
    """Mirroring a lineup is the host's own entrypoint, and printing the curated
    text served a config file no host names any more."""
    with pytest.raises(SystemExit):
        main(argv)
    assert f"invalid choice: '{argv[0]}'" in capsys.readouterr().err


# --- env -> sync -> ask round trip (mission #2) ---


def test_env_sync_ask_round_trip(tmp_path, capsys, monkeypatch):
    """A preset's api_key_ref surfaces through `env`, the app's own entrypoint syncs
    that same preset into a DB registry, and a broker over that DB routes a real call."""
    monkeypatch.chdir(tmp_path)
    body = (
        '[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="KEY_A"\n'
        '[keys.KEY_A]\nhelp="Get it at https://example.com/keys"\n'
    )
    monkeypatch.setattr(presets, "fetch_preset_text", lambda _name: body)
    db = str(tmp_path / "x.db")

    rc = main(["env", "freetier"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "KEY_A" in out
    assert "Get it at https://example.com/keys" in out

    monkeypatch.setenv("KEY_A", "test-key")

    async def run():
        broker = AsyncBroker(db)
        try:
            report = await broker.sync("freetier")
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

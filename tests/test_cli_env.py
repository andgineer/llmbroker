"""Tests for `env`'s output: file order, help lines, already-set annotation,
and the preset-name form that needs no local file.
"""

import urllib.error
from unittest.mock import MagicMock, patch

from llmbroker.cli import main

_PRESET_TOML = (
    b'[[llms]]\nname="x"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="FETCHED_KEY"\n'
    b"[keys.FETCHED_KEY]\n"
    b'help = "Sign up at example.com."\n'
)


def _mock_urlopen(content: bytes):
    resp = MagicMock()
    resp.read.return_value = content
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock()
    return resp


def _write_toml(tmp_path, body: str) -> str:
    f = tmp_path / "llms.toml"
    f.write_text(body)
    return str(f)


def test_shipped_preset_prints_in_file_order(capsys):
    rc = main(["env", "src/llmbroker/presets/freetier.toml"])
    out = capsys.readouterr().out
    assert rc == 0
    order = [ref for ref in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY") if ref in out]
    positions = [out.index(f"{ref}=") for ref in order]
    assert positions == sorted(positions)
    assert order == ["GROQ_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY"]


def test_refs_print_in_llms_declaration_order(tmp_path, capsys):
    body = (
        '[[llms]]\nname="a"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="ZZZ_KEY"\n'
        '[[llms]]\nname="b"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="AAA_KEY"\n'
    )
    rc = main(["env", _write_toml(tmp_path, body)])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.index("ZZZ_KEY=") < out.index("AAA_KEY=")


def test_help_line_printed_before_ref(tmp_path, capsys):
    body = (
        '[[llms]]\nname="a"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="KEY_A"\n'
        "[keys.KEY_A]\n"
        'help = "Create an account."\n'
    )
    rc = main(["env", _write_toml(tmp_path, body)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "# KEY_A — Create an account." in out
    assert out.index("# KEY_A — Create an account.") < out.index("KEY_A=")


def test_already_set_env_var_is_annotated(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("KEY_A", "already-here")
    body = '[[llms]]\nname="a"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="KEY_A"\n'
    rc = main(["env", _write_toml(tmp_path, body)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "# KEY_A already set" in out
    assert "KEY_A=" not in out


def test_preset_name_is_fetched_when_no_such_file_exists(capsys, monkeypatch):
    monkeypatch.delenv("FETCHED_KEY", raising=False)
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_PRESET_TOML)) as urlopen:
        rc = main(["env", "freetier"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "FETCHED_KEY=" in out
    assert "# FETCHED_KEY — Sign up at example.com." in out
    assert "src/llmbroker/presets/freetier.toml" in urlopen.call_args[0][0]


def test_existing_file_wins_over_a_same_named_preset(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "freetier").write_text("")  # not a real config, but it exists
    body = '[[llms]]\nname="a"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="LOCAL_KEY"\n'
    (tmp_path / "local.toml").write_text(body)
    with patch("urllib.request.urlopen", side_effect=AssertionError("must not fetch")):
        rc = main(["env", "local.toml"])
    assert rc == 0
    assert "LOCAL_KEY=" in capsys.readouterr().out


def test_missing_file_that_is_not_a_preset_name_errors_clearly(capsys):
    rc = main(["env", "no/such/config.toml"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "no such file" in err
    assert "not a valid preset name" in err


def test_unknown_preset_name_reports_the_catalog_miss(capsys):
    exc = urllib.error.HTTPError(url="u", code=404, msg="Not Found", hdrs=None, fp=None)
    with patch("urllib.request.urlopen", side_effect=exc):
        rc = main(["env", "nosuchpreset"])
    assert rc == 1
    assert "not found in catalog" in capsys.readouterr().err


def test_extra_fields_do_not_appear_in_output(tmp_path, capsys):
    """extra is a passthrough for host code — the env command only prints help lines."""
    body = (
        '[[llms]]\nname="a"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="KEY_A"\n'
        "[keys.KEY_A]\n"
        'effort = "signup"\n'
        'value = "good"\n'
        'help = "Create an account."\n'
    )
    rc = main(["env", _write_toml(tmp_path, body)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "effort" not in out
    assert "value=good" not in out

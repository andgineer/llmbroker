"""Tests for `env`'s output: declaration order, help lines, and its one form — a
curated preset name.
"""

import urllib.error
from unittest.mock import MagicMock, patch

import pytest

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


def _preset(body: str):
    return patch("urllib.request.urlopen", return_value=_mock_urlopen(body.encode()))


def test_shipped_preset_prints_in_file_order(capsys, bundled_presets):
    rc = main(["env", "freetier"])
    out = capsys.readouterr().out
    assert rc == 0
    order = [ref for ref in ("GROQ_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY") if ref in out]
    positions = [out.index(f"{ref}=") for ref in order]
    assert positions == sorted(positions)
    assert order == ["GROQ_API_KEY", "OPENROUTER_API_KEY", "GEMINI_API_KEY"]


def test_refs_print_in_llms_declaration_order(capsys):
    body = (
        '[[llms]]\nname="a"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="ZZZ_KEY"\n'
        '[[llms]]\nname="b"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="AAA_KEY"\n'
    )
    with _preset(body):
        rc = main(["env", "freetier"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.index("ZZZ_KEY=") < out.index("AAA_KEY=")


def test_one_line_per_distinct_ref(capsys):
    body = (
        '[[llms]]\nname="a"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="ONE_KEY"\n'
        '[[llms]]\nname="b"\nbase_url="https://y/v1"\nmodel="m"\napi_key_ref="ONE_KEY"\n'
    )
    with _preset(body):
        rc = main(["env", "freetier"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("ONE_KEY=") == 1


def test_help_line_printed_before_ref(capsys):
    body = (
        '[[llms]]\nname="a"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="KEY_A"\n'
        "[keys.KEY_A]\n"
        'help = "Create an account."\n'
    )
    with _preset(body):
        rc = main(["env", "freetier"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "# KEY_A — Create an account." in out
    assert out.index("# KEY_A — Create an account.") < out.index("KEY_A=")


def test_a_ref_without_a_keys_entry_still_gets_its_line(capsys):
    body = (
        '[[llms]]\nname="a"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="HELPED_KEY"\n'
        '[[llms]]\nname="b"\nbase_url="https://y/v1"\nmodel="m"\napi_key_ref="BARE_KEY"\n'
        "[keys.HELPED_KEY]\n"
        'help = "Create an account."\n'
    )
    with _preset(body):
        rc = main(["env", "freetier"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "BARE_KEY=" in out
    assert "# BARE_KEY" not in out


def test_output_does_not_depend_on_the_environment(capsys, monkeypatch):
    """A generator whose output depended on the process's own environment would emit a
    different skeleton than the file it is generating is meant to supply."""
    body = '[[llms]]\nname="a"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="KEY_A"\n'
    monkeypatch.delenv("KEY_A", raising=False)
    with _preset(body):
        assert main(["env", "freetier"]) == 0
    unset = capsys.readouterr().out

    monkeypatch.setenv("KEY_A", "already-here")
    with _preset(body):
        assert main(["env", "freetier"]) == 0
    already_set = capsys.readouterr().out

    assert unset == already_set
    assert "KEY_A=" in unset


def test_no_argument_is_a_usage_error_naming_the_preset(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["env"])
    assert exc.value.code == 2
    assert "preset" in capsys.readouterr().err


def test_a_path_where_a_preset_name_belongs_errors_clearly(capsys):
    rc = main(["env", "no/such/config.toml"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "not a valid preset name" in err


def test_unknown_preset_name_reports_the_catalog_miss(capsys):
    exc = urllib.error.HTTPError(url="u", code=404, msg="Not Found", hdrs=None, fp=None)
    with patch("urllib.request.urlopen", side_effect=exc):
        rc = main(["env", "nosuchpreset"])
    assert rc == 1
    assert "not found in catalog" in capsys.readouterr().err


def test_extra_fields_do_not_appear_in_output(capsys):
    """extra is a passthrough for host code — the env command only prints help lines."""
    body = (
        '[[llms]]\nname="a"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="KEY_A"\n'
        "[keys.KEY_A]\n"
        'effort = "signup"\n'
        'value = "good"\n'
        'help = "Create an account."\n'
    )
    with _preset(body):
        rc = main(["env", "freetier"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "effort" not in out
    assert "value=good" not in out


def test_a_malformed_preset_is_an_error_line_not_a_traceback(capsys):
    body = (
        '[[llms]]\nname="a"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="KEY_A"\n'
        '[[llms]]\nname="a"\nbase_url="https://y/v1"\nmodel="m"\napi_key_ref="KEY_B"\n'
    )
    with _preset(body):
        rc = main(["env", "freetier"])
    assert rc == 1
    assert "error: Registry: duplicate name 'a'" in capsys.readouterr().err


def test_a_malformed_keys_table_is_an_error_line(capsys):
    body = (
        'keys = "KEY_A"\n'
        '[[llms]]\nname="a"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="KEY_A"\n'
    )
    with _preset(body):
        rc = main(["env", "freetier"])
    assert rc == 1
    assert "[keys] is str, not a table" in capsys.readouterr().err


def test_the_fetched_preset_is_the_one_named(capsys, monkeypatch):
    monkeypatch.delenv("FETCHED_KEY", raising=False)
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_PRESET_TOML)) as urlopen:
        rc = main(["env", "freetier"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "FETCHED_KEY=" in out
    assert "# FETCHED_KEY — Sign up at example.com." in out
    assert "src/llmbroker/presets/freetier.toml" in urlopen.call_args[0][0]

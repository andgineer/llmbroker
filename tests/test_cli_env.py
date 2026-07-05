"""Tests for `env`'s output: file order, help lines, already-set annotation."""

from llmbroker.cli import main


def _write_toml(tmp_path, body: str) -> str:
    f = tmp_path / "llms.toml"
    f.write_text(body)
    return str(f)


def test_shipped_preset_prints_in_file_order(capsys):
    rc = main(["env", "presets/freetier.toml"])
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

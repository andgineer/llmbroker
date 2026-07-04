"""Tests for `env`'s onboarding ordering: effort/value sort and annotations."""

from llmbroker.cli import main


def _write_toml(tmp_path, body: str) -> str:
    f = tmp_path / "llms.toml"
    f.write_text(body)
    return str(f)


def test_shipped_preset_orders_gemini_groq_openrouter(capsys):
    rc = main(["env", "presets/freetier.toml"])
    out = capsys.readouterr().out
    assert rc == 0
    order = [ref for ref in ("GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY") if ref in out]
    positions = [out.index(f"{ref}=") for ref in order]
    assert positions == sorted(positions)
    assert order == ["GEMINI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"]


def test_same_effort_sorts_by_value_not_alphabetically(tmp_path, capsys):
    # Alphabetically "AAA_KEY" < "ZZZ_KEY", but ZZZ_KEY has the better value and must sort first.
    body = (
        '[[llms]]\nname="a"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="AAA_KEY"\n'
        '[[llms]]\nname="b"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="ZZZ_KEY"\n'
        "[keys.AAA_KEY]\n"
        'effort = "signup"\n'
        'value = "niche"\n'
        'help = "a"\n'
        "[keys.ZZZ_KEY]\n"
        'effort = "signup"\n'
        'value = "high"\n'
        'help = "z"\n'
    )
    rc = main(["env", _write_toml(tmp_path, body)])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.index("ZZZ_KEY=") < out.index("AAA_KEY=")


def test_unknown_value_sorts_after_known_at_same_effort(tmp_path, capsys):
    body = (
        '[[llms]]\nname="a"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="KNOWN_KEY"\n'
        '[[llms]]\nname="b"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="UNKNOWN_KEY"\n'
        "[keys.KNOWN_KEY]\n"
        'effort = "oauth"\n'
        'value = "niche"\n'
        'help = "k"\n'
        "[keys.UNKNOWN_KEY]\n"
        'effort = "oauth"\n'
        'value = "not-a-real-level"\n'
        'help = "u"\n'
    )
    rc = main(["env", _write_toml(tmp_path, body)])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.index("KNOWN_KEY=") < out.index("UNKNOWN_KEY=")


def test_same_effort_and_value_sorts_alphabetically_by_ref(tmp_path, capsys):
    body = (
        '[[llms]]\nname="a"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="ZZZ_KEY"\n'
        '[[llms]]\nname="b"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="AAA_KEY"\n'
        "[keys.ZZZ_KEY]\n"
        'effort = "signup"\n'
        'value = "good"\n'
        'help = "z"\n'
        "[keys.AAA_KEY]\n"
        'effort = "signup"\n'
        'value = "good"\n'
        'help = "a"\n'
    )
    rc = main(["env", _write_toml(tmp_path, body)])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.index("AAA_KEY=") < out.index("ZZZ_KEY=")


def test_already_set_env_var_is_annotated(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("KEY_A", "already-here")
    body = '[[llms]]\nname="a"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="KEY_A"\n'
    rc = main(["env", _write_toml(tmp_path, body)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "# KEY_A already set" in out
    assert "KEY_A=" not in out


def test_annotations_render_effort_and_value(tmp_path, capsys):
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
    assert "effort=signup" in out
    assert "value=good" in out

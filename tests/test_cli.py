"""Unit tests for the CLI (python -m llmbroker)."""

from llmbroker.cli import main


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

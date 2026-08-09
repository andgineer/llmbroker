"""Unit tests for the CLI (python -m llmbroker)."""

import asyncio
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


def test_env_reports_a_leftover_declared_section(llmbroker_home, capsys):
    """A file a previous release wrote is refused, not half-read, and the message
    says where those models go now."""
    _lineup(
        llmbroker_home,
        '[[llms]]\nname="pool"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="POOL_KEY"\n'
        '[[custom]]\nname="frontier"\nbase_url="https://y/v1"\nmodel="big"'
        '\napi_key_ref="PAID_KEY"\n',
    )
    rc = main(["env"])
    assert rc == 1
    assert "direct=[...]" in capsys.readouterr().err


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


# --- list: the curated model lists, read-only ---

_CATALOG = (
    b'[[provider]]\nid="anthropic"\nlabel="Anthropic"\n'
    b'base_url="https://api.anthropic.com/v1"\napi_key_ref="ANTHROPIC_API_KEY"\n'
    b'key_help="console.anthropic.com"\n'
    b'  [[provider.models]]\n  alias="opus"\n  model="claude-opus-4-8"\n'
    b'  label="Opus"\n  verified="u"\n'
    b'  [[provider.models]]\n  alias="sonnet"\n  model="claude-sonnet-5"\n'
    b'  label="Sonnet"\n  verified="u"\n'
)

_POOL = (
    b'[[llms]]\nname="groq-a"\nbase_url="https://api.groq.com/openai/v1"\n'
    b'model="oss-120b"\napi_key_ref="GROQ_API_KEY"\nweight=0.5\n'
    b'[[llms]]\nname="google-b"\nbase_url="https://gen.googleapis.com/v1beta/openai"\n'
    b'model="gemini-flash"\napi_key_ref="GEMINI_API_KEY"\nweight=0.6\n'
)


def _lineup(
    home, body='[[llms]]\nname="pool"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="POOL_KEY"\n'
):
    """The model list lives inside llmbroker's own directory, which the autouse
    fixture points at a temp dir."""
    f = home / "lineup.toml"
    f.write_text(body)
    return f


def _catalogs(name: str) -> bytes:
    return _POOL if "freetier" in name else _CATALOG


def _fetch_both():
    """`list` reads both curated presets, so the fake fetch answers by URL."""
    return patch(
        "urllib.request.urlopen",
        side_effect=lambda url, **_kw: _mock_urlopen(_catalogs(str(url))),
    )


def test_list_shows_both_curated_presets(llmbroker_home, capsys):
    with _fetch_both():
        rc = main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "pool groq-a oss-120b https://api.groq.com/openai/v1 GROQ_API_KEY" in out
    assert (
        "pool google-b gemini-flash https://gen.googleapis.com/v1beta/openai GEMINI_API_KEY" in out
    )
    assert "direct opus" in out
    assert "direct sonnet" in out


def test_list_carries_the_fields_a_pinned_declaration_needs(llmbroker_home, capsys):
    with _fetch_both():
        rc = main(["list"])
    assert rc == 0
    line = next(ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("direct opus "))
    assert line.split() == [
        "direct",
        "opus",
        "anthropic",
        "claude-opus-4-8",
        "https://api.anthropic.com/v1",
        "ANTHROPIC_API_KEY",
    ]


def test_list_marks_a_catalog_model_with_no_alias(llmbroker_home, capsys):
    catalog = (
        b'[[provider]]\nid="x"\nlabel="X"\nbase_url="https://x/v1"\napi_key_ref="X_KEY"\n'
        b'  [[provider.models]]\n  model="m1"\n  label="M1"\n  verified="u"\n'
    )
    with patch(
        "urllib.request.urlopen",
        side_effect=lambda url, **_kw: _mock_urlopen(_POOL if "freetier" in str(url) else catalog),
    ):
        rc = main(["list"])
    assert rc == 0
    assert "direct - x m1 https://x/v1 X_KEY" in capsys.readouterr().out


def test_list_writes_nothing(llmbroker_home, capsys):
    f = _lineup(llmbroker_home)
    before = f.read_text()
    with _fetch_both():
        rc = main(["list"])
    assert rc == 0
    assert f.read_text() == before


def test_list_offline_reads_the_bundled_presets(llmbroker_home, capsys, bundled_presets):
    """Both curated lists ship in the wheel, so listing them needs no network."""
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError(reason="offline")):
        rc = main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "direct opus anthropic" in out
    assert any(ln.startswith("pool ") for ln in out.splitlines())


def test_list_reports_a_fetch_failure_with_nothing_to_fall_back_to(llmbroker_home, capsys):
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError(reason="offline")):
        rc = main(["list"])
    assert rc == 1
    assert "offline" in capsys.readouterr().err


# --- what the CLI is not ---


@pytest.mark.parametrize(
    "argv",
    [
        ["sync", "preset.toml", "broker.db"],
        ["preset", "freetier"],
        ["add-model", "--provider", "anthropic", "--model", "claude-opus-5"],
    ],
)
def test_the_removed_subcommands_stay_removed(capsys, argv):
    """Mirroring a model list is the host's own entrypoint, printing the curated text
    served a config file no host names any more, and a model reached by name is
    declared in code."""
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

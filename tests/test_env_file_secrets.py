"""`.env` support: the documented quickstart (`llmbroker env freetier > .env`)
must actually resolve keys, without adding a dependency.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from llmbroker.broker.broker import AsyncBroker
from llmbroker.standalone.secrets import Secrets, parse_env_file
from llmbroker.standalone.store import InMemoryStore

_PATCH = "llmbroker.broker.router.call_provider"


def _write_model_list(home):
    """The model list a zero-config broker with ``home=`` reads."""
    f = home / "model-list.toml"
    f.write_text('[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n')
    return f


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_env_file_skips_comments_blanks_and_malformed_lines():
    parsed = parse_env_file("# hint\n\nA=1\nno-equals-here\n  B = two \nexport C=3\n")
    assert parsed == {"A": "1", "B": "two", "C": "3"}


def test_parse_env_file_strips_matching_quotes_only():
    parsed = parse_env_file("A=\"quoted\"\nB='single'\nC=\"mismatched'\n")
    assert parsed == {"A": "quoted", "B": "single", "C": "\"mismatched'"}


# ---------------------------------------------------------------------------
# Secrets(env_file=...)
# ---------------------------------------------------------------------------


def test_file_only_ref_resolves(tmp_path):
    (tmp_path / ".env").write_text("K=from-file\n")
    assert asyncio.run(Secrets(tmp_path / ".env").resolve("K")) == "from-file"


def test_real_environment_wins_over_the_file(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("K=from-file\n")
    monkeypatch.setenv("K", "from-env")
    assert asyncio.run(Secrets(tmp_path / ".env").resolve("K")) == "from-env"


def test_missing_file_behaves_like_no_file(tmp_path):
    with pytest.raises(KeyError, match="NOPE"):
        asyncio.run(Secrets(tmp_path / "absent.env").resolve("NOPE"))


def test_no_env_file_configured_is_unchanged(monkeypatch):
    monkeypatch.setenv("K", "from-env")
    assert asyncio.run(Secrets().resolve("K")) == "from-env"
    with pytest.raises(KeyError):
        asyncio.run(Secrets().resolve("MISSING"))


def test_malformed_lines_are_skipped_not_fatal(tmp_path):
    (tmp_path / ".env").write_text("garbage line\nK=good\n")
    assert asyncio.run(Secrets(tmp_path / ".env").resolve("K")) == "good"


def test_unfilled_skeleton_line_is_not_a_key(tmp_path):
    """`llmbroker env` writes `K=`; an unfilled one must leave the model keyless
    rather than resolve to an empty credential the provider will 401."""
    (tmp_path / ".env").write_text("# K — get it at https://example\nK=\n", encoding="utf-8")
    with pytest.raises(KeyError, match="K"):
        asyncio.run(Secrets(tmp_path / ".env").resolve("K"))


def test_inline_comment_is_not_part_of_an_unquoted_value(tmp_path):
    (tmp_path / ".env").write_text('K=sk-abc  # personal key\nQ="sk-x # kept"\n')
    secrets = Secrets(tmp_path / ".env")
    assert asyncio.run(secrets.resolve("K")) == "sk-abc"
    assert asyncio.run(secrets.resolve("Q")) == "sk-x # kept"


def test_file_is_reread_when_it_changes(tmp_path):
    """A key filled in while the broker runs takes effect on the next resolve,
    exactly as exporting the variable would."""
    env = tmp_path / ".env"
    env.write_text("K=\n")
    secrets = Secrets(env)
    with pytest.raises(KeyError):
        asyncio.run(secrets.resolve("K"))

    env.write_text("K=filled-in-later\n")
    assert asyncio.run(secrets.resolve("K")) == "filled-in-later"


def test_quickstart_skeleton_leaves_the_model_inactive(tmp_path, monkeypatch):
    """The whole generated skeleton, nothing filled in: the pool must be empty of
    keys, not full of models routing with an empty credential."""
    _write_model_list(tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("# K — get it at https://example\nK=\n", encoding="utf-8")
    monkeypatch.delenv("K", raising=False)

    async def run():
        async with AsyncBroker(home=tmp_path, sync=None, store=InMemoryStore()) as broker:
            assert broker._pool.config("p1").api_key_ref not in broker._catalog.payable

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Wiring: the working directory's .env
# ---------------------------------------------------------------------------


def test_quickstart_tree_resolves_keys_without_exported_vars(tmp_path, monkeypatch):
    """`llmbroker env freetier > .env`, nothing exported: a zero-config broker routes."""
    _write_model_list(tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("# GROQ hint\nK=sk-from-dot-env\n")
    monkeypatch.delenv("K", raising=False)

    async def run():
        async with AsyncBroker(home=tmp_path, sync=None, store=InMemoryStore()) as broker:
            with patch(_PATCH, new=AsyncMock(return_value=("ok", None, None))):
                result = await broker.ask("hi")
            assert result.text == "ok"
            assert (
                await broker._shared_ring.resolve(broker._pool.config("p1").api_key_ref)
                == "sk-from-dot-env"
            )

    asyncio.run(run())


def test_the_env_file_is_the_working_directorys_not_the_model_lists_sibling(tmp_path, monkeypatch):
    """A `.env` beside the model list in llmbroker's own home is not read: keys come from
    the environment, with the working directory's `.env` behind them."""
    home = tmp_path / "home"
    home.mkdir()
    _write_model_list(home)
    (home / ".env").write_text("K=beside-the-model list\n")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("K", raising=False)

    async def run():
        async with AsyncBroker(home=home, sync=None, store=InMemoryStore()) as broker:
            assert broker._pool.config("p1").api_key_ref not in broker._catalog.payable

    asyncio.run(run())

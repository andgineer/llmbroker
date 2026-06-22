"""Tests for the file-backed Registry (TOML and JSON)."""

import asyncio
import json

import llmbroker.sqlite
import pytest

from llmbroker.models import LLMConfig
from llmbroker.standalone.registry import Registry


def test_load_toml(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text(
        '[[llms]]\nname="groq"\nbase_url="https://api.groq.com/v1"\nmodel="llama"\napi_key_ref="K"\n'
    )
    configs = asyncio.run(Registry(f).load())
    assert len(configs) == 1
    assert configs[0].name == "groq"
    assert configs[0].base_url == "https://api.groq.com/v1"
    assert configs[0].model == "llama"
    assert configs[0].api_key_ref == "K"


def test_load_json(tmp_path):
    f = tmp_path / "llms.json"
    f.write_text(
        json.dumps(
            {"llms": [{"name": "g", "base_url": "https://x/v1", "model": "m", "api_key_ref": "K"}]}
        )
    )
    configs = asyncio.run(Registry(f).load())
    assert len(configs) == 1
    assert configs[0].name == "g"


def test_load_multiple_entries(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text(
        '[[llms]]\nname="a"\nbase_url="https://a/v1"\nmodel="m"\napi_key_ref="A"\n'
        '[[llms]]\nname="b"\nbase_url="https://b/v1"\nmodel="m"\napi_key_ref="B"\n'
    )
    configs = asyncio.run(Registry(f).load())
    assert [c.name for c in configs] == ["a", "b"]


def test_load_missing_file_returns_empty(tmp_path):
    configs = asyncio.run(Registry(tmp_path / "nope.toml").load())
    assert configs == []


def test_load_unsupported_extension_raises(tmp_path):
    f = tmp_path / "llms.yaml"
    f.write_text("")
    with pytest.raises(ValueError, match="unsupported config extension"):
        asyncio.run(Registry(f).load())


def test_load_skips_entry_without_name(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text('[[llms]]\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n')
    assert asyncio.run(Registry(f).load()) == []


def test_load_skips_entry_without_base_url(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text('[[llms]]\nname="g"\nmodel="m"\napi_key_ref="K"\n')
    assert asyncio.run(Registry(f).load()) == []


def test_load_empty_llms_section(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text("")
    assert asyncio.run(Registry(f).load()) == []


# ── SQLite registry per-user scoping tests ────────────────────────────────────


def _cfg(name: str, url: str = "https://x/v1") -> LLMConfig:
    return LLMConfig(name=name, base_url=url, model="m", api_key_ref="K")


def test_sqlite_registry_per_user_row_isolated(tmp_path):
    """Per-user rows are not visible to other users."""
    db = str(tmp_path / "b.db")
    reg = llmbroker.sqlite.Registry(db)

    async def run():
        await reg.add(_cfg("alice-llm"), "alice")
        alice_rows = await reg.load("alice")
        bob_rows = await reg.load("bob")
        assert len(alice_rows) == 1
        assert len(bob_rows) == 0

    asyncio.run(run())


def test_sqlite_registry_same_name_different_users_allowed(tmp_path):
    """Two users can have rows with the same name without conflict."""
    db = str(tmp_path / "b.db")
    reg = llmbroker.sqlite.Registry(db)

    async def run():
        await reg.add(_cfg("llm", "https://a/v1"), "alice")
        await reg.add(_cfg("llm", "https://b/v1"), "bob")
        alice_rows = await reg.load("alice")
        bob_rows = await reg.load("bob")
        assert alice_rows[0].base_url == "https://a/v1"
        assert bob_rows[0].base_url == "https://b/v1"

    asyncio.run(run())


def test_sqlite_registry_duplicate_within_user_rejected(tmp_path):
    """Adding the same name twice for the same user raises ValueError."""
    db = str(tmp_path / "b.db")
    reg = llmbroker.sqlite.Registry(db)

    async def run():
        await reg.add(_cfg("llm"), "alice")
        with pytest.raises(ValueError, match="already exists"):
            await reg.add(_cfg("llm"), "alice")

    asyncio.run(run())


def test_sqlite_registry_load_none_returns_only_unscoped(tmp_path):
    """load(None) returns only NULL-scoped rows and does not bleed named-user rows."""
    db = str(tmp_path / "b.db")
    reg = llmbroker.sqlite.Registry(db)

    async def run():
        await reg.add(_cfg("shared"))
        await reg.add(_cfg("alice-llm"), "alice")
        none_rows = await reg.load()
        alice_rows = await reg.load("alice")
        assert [r.name for r in none_rows] == ["shared"]
        assert [r.name for r in alice_rows] == ["alice-llm"]

    asyncio.run(run())


def test_sqlite_registry_get_existing(tmp_path):
    db = str(tmp_path / "b.db")
    reg = llmbroker.sqlite.Registry(db)

    async def run():
        await reg.add(_cfg("p1", "https://x/v1"))
        result = await reg.get("p1")
        assert result is not None
        assert result.name == "p1"
        assert result.base_url == "https://x/v1"

    asyncio.run(run())


def test_sqlite_registry_get_missing_returns_none(tmp_path):
    db = str(tmp_path / "b.db")
    reg = llmbroker.sqlite.Registry(db)

    async def run():
        assert await reg.get("ghost") is None

    asyncio.run(run())


def test_sqlite_registry_update_missing_raises_key_error(tmp_path):
    db = str(tmp_path / "b.db")
    reg = llmbroker.sqlite.Registry(db)

    async def run():
        with pytest.raises(KeyError):
            await reg.update(_cfg("ghost"))

    asyncio.run(run())


def test_sqlite_registry_remove_missing_raises_key_error(tmp_path):
    db = str(tmp_path / "b.db")
    reg = llmbroker.sqlite.Registry(db)

    async def run():
        with pytest.raises(KeyError):
            await reg.remove("ghost")

    asyncio.run(run())

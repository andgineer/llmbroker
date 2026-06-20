"""Tests for the file-backed Registry (TOML and JSON)."""

import asyncio
import json

import pytest

from llmbroker.registry import Registry


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

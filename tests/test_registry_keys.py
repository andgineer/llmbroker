"""Tests for per-model parallel cap and per-provider key_info parsing in the file Registry."""

import asyncio

from llmbroker.models import KeyInfo
from llmbroker.protocols.registry import KeyInfoProtocol
from llmbroker.standalone.registry import Registry


def test_llms_parallel_reaches_llmconfig(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text(
        '[[llms]]\nname="g"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\nparallel = 3\n',
    )
    configs = asyncio.run(Registry(f).load())
    assert configs[0].parallel == 3


def test_llms_without_parallel_is_none(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text('[[llms]]\nname="g"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n')
    configs = asyncio.run(Registry(f).load())
    assert configs[0].parallel is None


def test_nested_keys_table_parses_help_and_extra_passthrough(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text(
        '[[llms]]\nname="g"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n'
        "[keys.K]\n"
        'effort = "signup"\n'
        'value = "good"\n'
        'help = "Create a free account."\n',
    )
    info = asyncio.run(Registry(f).key_info())
    assert info == {
        "K": KeyInfo(
            api_key_ref="K",
            help="Create a free account.",
            extra={"effort": "signup", "value": "good"},
        ),
    }


def test_flat_string_keys_entry_is_help_only(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text(
        '[[llms]]\nname="g"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n'
        '[keys]\nK = "Get it at https://example.com/keys"\n',
    )
    info = asyncio.run(Registry(f).key_info())
    assert info == {
        "K": KeyInfo(api_key_ref="K", help="Get it at https://example.com/keys", extra={}),
    }


def test_arbitrary_extra_fields_pass_through_unvalidated(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text(
        '[[llms]]\nname="g"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n'
        '[keys.K]\nanything = "goes"\nhelp = "x"\n',
    )
    info = asyncio.run(Registry(f).key_info())
    assert info["K"].extra == {"anything": "goes"}


def test_key_info_absent_returns_empty(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text('[[llms]]\nname="g"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n')
    assert asyncio.run(Registry(f).key_info()) == {}


def test_key_info_missing_file_returns_empty(tmp_path):
    assert asyncio.run(Registry(tmp_path / "nope.toml").key_info()) == {}


def test_registry_satisfies_key_info_protocol(tmp_path):
    assert isinstance(Registry(tmp_path / "x.toml"), KeyInfoProtocol)


# ── Shipped catalog (presets/freetier.toml) ──────────────────────────────────


def test_shipped_freetier_preset_configs_load():
    configs = asyncio.run(Registry("presets/freetier.toml").load())
    assert len(configs) == 3


def test_shipped_freetier_preset_key_info_extra_passthrough():
    info = asyncio.run(Registry("presets/freetier.toml").key_info())
    assert len(info) == 3
    assert info["GEMINI_API_KEY"].extra == {"effort": "oauth", "value": "high"}
    assert info["GROQ_API_KEY"].extra == {"effort": "signup", "value": "good"}
    assert info["OPENROUTER_API_KEY"].extra == {"effort": "signup", "value": "good"}

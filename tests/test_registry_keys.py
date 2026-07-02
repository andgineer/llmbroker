"""Tests for per-model rate_limit and per-provider key_info parsing in the file Registry."""

import asyncio

from llmbroker.models import EffortLevel, KeyInfo, RateLimit, ValueLevel
from llmbroker.protocols.registry import KeyInfoProtocol
from llmbroker.standalone.registry import Registry


def test_llms_rate_limit_reaches_llmconfig(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text(
        '[[llms]]\nname="g"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n'
        "rate_limit = { rpm = 30, rpd = 1000 }\n",
    )
    configs = asyncio.run(Registry(f).load())
    assert configs[0].rate_limit == RateLimit(rpm=30, rpd=1000)


def test_llms_without_rate_limit_is_none(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text('[[llms]]\nname="g"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n')
    configs = asyncio.run(Registry(f).load())
    assert configs[0].rate_limit is None


def test_nested_keys_table_parses_effort_value_help(tmp_path):
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
            effort=EffortLevel.SIGNUP,
            value=ValueLevel.GOOD,
            help="Create a free account.",
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
        "K": KeyInfo(
            api_key_ref="K", effort=None, value=None, help="Get it at https://example.com/keys"
        ),
    }


def test_unknown_effort_becomes_none(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text(
        '[[llms]]\nname="g"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n'
        '[keys.K]\neffort = "bogus"\nvalue = "good"\nhelp = "x"\n',
    )
    info = asyncio.run(Registry(f).key_info())
    assert info["K"].effort is None
    assert info["K"].value is ValueLevel.GOOD


def test_unknown_value_becomes_none(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text(
        '[[llms]]\nname="g"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n'
        '[keys.K]\neffort = "oauth"\nvalue = "bogus"\nhelp = "x"\n',
    )
    info = asyncio.run(Registry(f).key_info())
    assert info["K"].value is None
    assert info["K"].effort is EffortLevel.OAUTH


def test_key_info_absent_returns_empty(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text('[[llms]]\nname="g"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n')
    assert asyncio.run(Registry(f).key_info()) == {}


def test_key_info_missing_file_returns_empty(tmp_path):
    assert asyncio.run(Registry(tmp_path / "nope.toml").key_info()) == {}


def test_registry_satisfies_key_info_protocol(tmp_path):
    assert isinstance(Registry(tmp_path / "x.toml"), KeyInfoProtocol)


# ── Shipped catalog (presets/freetier.toml) ──────────────────────────────────


def test_shipped_freetier_preset_configs_carry_rate_limit():
    configs = asyncio.run(Registry("presets/freetier.toml").load())
    assert len(configs) == 3
    assert all(c.rate_limit is not None for c in configs)


def test_shipped_freetier_preset_key_info_effort_and_value():
    info = asyncio.run(Registry("presets/freetier.toml").key_info())
    assert len(info) == 3
    assert info["GEMINI_API_KEY"].effort is EffortLevel.OAUTH
    assert info["GEMINI_API_KEY"].value is ValueLevel.HIGH
    assert info["GROQ_API_KEY"].effort is EffortLevel.SIGNUP
    assert info["GROQ_API_KEY"].value is ValueLevel.GOOD
    assert info["OPENROUTER_API_KEY"].effort is EffortLevel.SIGNUP
    assert info["OPENROUTER_API_KEY"].value is ValueLevel.GOOD

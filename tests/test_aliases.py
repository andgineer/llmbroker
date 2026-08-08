"""The paid catalog's targets, and re-pointing what follows an alias."""

import pytest

from llmbroker.broker import presets
from llmbroker.broker.aliases import (
    AliasChange,
    AliasFact,
    alias_targets_for,
    catalog_alias_targets,
    refresh_alias_configs,
)
from llmbroker.broker.presets import PresetSource
from llmbroker.models import LLMConfig

_CATALOG = {
    "provider": [
        {
            "id": "anthropic",
            "base_url": "https://api.anthropic.com/v2",
            "api_key_ref": "ANTHROPIC_API_KEY",
            "key_help": "console.anthropic.com",
            "models": [{"alias": "opus", "model": "claude-opus-5"}],
        },
    ],
}


def _opus(**kw) -> LLMConfig:
    fields = {
        "name": "anthropic-claude-opus-4-8",
        "base_url": "https://api.anthropic.com/v1",
        "model": "claude-opus-4-8",
        "api_key_ref": "ANTHROPIC_API_KEY",
        "custom": True,
        "alias": "opus",
    }
    return LLMConfig(**{**fields, **kw})


def test_a_duplicate_catalog_alias_is_an_invalid_catalog():
    catalog = {
        "provider": [
            *_CATALOG["provider"],
            {
                "id": "dup",
                "base_url": "https://dup/v1",
                "api_key_ref": "D",
                "models": [{"alias": "opus", "model": "dup-1"}],
            },
        ],
    }
    with pytest.raises(ValueError, match="alias 'opus' is used twice"):
        catalog_alias_targets(catalog)


def test_alias_entries_follow_the_catalog():
    stored = [
        _opus(),
        LLMConfig(name="gemini", base_url="https://g/v1", model="m", api_key_ref="G"),
    ]
    refreshed, refresh = refresh_alias_configs(stored, catalog_alias_targets(_CATALOG))
    assert (refreshed[0].model, refreshed[0].name) == (
        "claude-opus-5",
        "anthropic-claude-opus-5",
    )
    assert refreshed[0].base_url == "https://api.anthropic.com/v2"
    assert refreshed[1] is stored[1]  # nothing without an alias is touched
    assert refresh.key_help == {"ANTHROPIC_API_KEY": "console.anthropic.com"}
    assert refresh.facts == (
        AliasFact(
            change=AliasChange.MODEL,
            alias="opus",
            was="claude-opus-4-8",
            now="claude-opus-5",
        ),
    )


def test_an_unknown_alias_is_a_fact_and_the_entry_is_left_alone():
    stored = [_opus(alias="ghost")]
    refreshed, refresh = refresh_alias_configs(stored, catalog_alias_targets(_CATALOG))
    assert refreshed == stored
    assert refresh.facts == (AliasFact(change=AliasChange.UNKNOWN, alias="ghost"),)


def test_a_re_spelled_key_ref_is_its_own_fact():
    """It can arrive without a model change at all, and it is the one thing the user
    has to act on."""
    stored = [_opus(name="anthropic-claude-opus-5", model="claude-opus-5", api_key_ref="OLD_KEY")]
    _refreshed, refresh = refresh_alias_configs(stored, catalog_alias_targets(_CATALOG))
    assert refresh.facts == (
        AliasFact(
            change=AliasChange.KEY_REF,
            alias="opus",
            was="OLD_KEY",
            now="ANTHROPIC_API_KEY",
        ),
    )


async def test_the_catalog_is_only_read_when_something_follows_an_alias(monkeypatch):
    """The catalog is a second network read; a lineup with nothing to follow must not
    pay for it."""

    def _boom(_name):
        raise AssertionError("the paid catalog was fetched with no alias to follow")

    monkeypatch.setattr(presets, "fetch_preset_text", _boom)
    assert await alias_targets_for([None, None], PresetSource()) == {}


async def test_an_unreachable_catalog_yields_no_targets(monkeypatch, caplog):
    """Not the wheel's copy: a refresh that cannot see upstream has nothing to say
    about where an alias points."""

    def _offline(_name):
        raise ValueError("offline")

    monkeypatch.setattr(presets, "fetch_preset_text", _offline)
    assert await alias_targets_for(["opus"], PresetSource()) == {}
    assert "alias entries left untouched" in caplog.text

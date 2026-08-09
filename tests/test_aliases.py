"""The paid catalog's targets, and what a re-resolution of `direct=` reports moved."""

import pytest

from llmbroker.broker.aliases import (
    AliasChange,
    AliasFact,
    catalog_alias_targets,
    resolve_declared,
)
from llmbroker.broker.presets import PresetSource
from llmbroker.models import DeclaredModels, LLMConfig

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

_CATALOG_TEXT = (
    '[[provider]]\nid="anthropic"\nbase_url="https://api.anthropic.com/v2"\n'
    'api_key_ref="ANTHROPIC_API_KEY"\nkey_help="console.anthropic.com"\n'
    '  [[provider.models]]\n  alias="opus"\n  model="claude-opus-5"\n'
)


def _opus(**kw) -> LLMConfig:
    fields = {
        "name": "anthropic-claude-opus-4-8",
        "base_url": "https://api.anthropic.com/v1",
        "model": "claude-opus-4-8",
        "api_key_ref": "ANTHROPIC_API_KEY",
        "alias": "opus",
    }
    return LLMConfig(**{**fields, **kw})


def _served(text: str, monkeypatch) -> PresetSource:
    monkeypatch.setattr("llmbroker.broker.presets.fetch_preset_text", lambda _name: text)
    return PresetSource()


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


async def test_a_declared_alias_resolves_from_the_catalog(monkeypatch):
    resolved, facts = await resolve_declared(["opus"], _served(_CATALOG_TEXT, monkeypatch))
    (cfg,) = resolved.configs
    assert (cfg.name, cfg.model, cfg.base_url) == (
        "anthropic-claude-opus-5",
        "claude-opus-5",
        "https://api.anthropic.com/v2",
    )
    assert resolved.key_help == {"ANTHROPIC_API_KEY": "console.anthropic.com"}
    assert facts == ()  # the first resolution has nothing to compare against


async def test_a_re_resolution_reports_the_model_that_moved(monkeypatch):
    previous = DeclaredModels(configs=(_opus(),))
    _resolved, facts = await resolve_declared(
        ["opus"],
        _served(_CATALOG_TEXT, monkeypatch),
        previous=previous,
    )
    assert facts == (
        AliasFact(
            change=AliasChange.MODEL,
            alias="opus",
            was="claude-opus-4-8",
            now="claude-opus-5",
        ),
    )


async def test_a_re_spelled_key_ref_is_its_own_fact(monkeypatch):
    """It can arrive without a model change at all, and it is the one thing the user
    has to act on."""
    previous = DeclaredModels(
        configs=(_opus(name="anthropic-claude-opus-5", model="claude-opus-5", api_key_ref="OLD"),),
    )
    _resolved, facts = await resolve_declared(
        ["opus"],
        _served(_CATALOG_TEXT, monkeypatch),
        previous=previous,
    )
    assert facts == (
        AliasFact(
            change=AliasChange.KEY_REF,
            alias="opus",
            was="OLD",
            now="ANTHROPIC_API_KEY",
        ),
    )


async def test_a_stated_config_moves_under_nobody_and_reports_nothing(monkeypatch):
    """Its version is the caller's to track, so a catalog move says nothing about it."""
    pinned = LLMConfig(name="mine", base_url="https://mine/v1", model="big", api_key_ref="K")
    resolved, facts = await resolve_declared(
        [pinned],
        _served(_CATALOG_TEXT, monkeypatch),
        previous=DeclaredModels(configs=(pinned,)),
    )
    assert resolved.configs == (pinned,)
    assert facts == ()


async def test_the_catalog_is_only_read_when_something_follows_an_alias(monkeypatch):
    """The catalog is a network read; a declaration with nothing to follow must not
    pay for it."""

    def _boom(_name):
        raise AssertionError("the paid catalog was fetched with no alias to follow")

    monkeypatch.setattr("llmbroker.broker.presets.fetch_preset_text", _boom)
    pinned = LLMConfig(name="mine", base_url="https://mine/v1", model="big", api_key_ref="K")
    resolved, _facts = await resolve_declared([pinned], PresetSource())
    assert resolved.configs == (pinned,)

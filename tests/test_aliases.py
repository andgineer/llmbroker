"""The paid catalog's targets, and what a re-resolution of `direct=` reports moved."""

from types import MappingProxyType

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


async def test_a_declared_alias_resolves_with_its_lines_tool_params(monkeypatch):
    text = _CATALOG_TEXT + '  tool_params = { reasoning_effort = "none" }\n'
    resolved, _facts = await resolve_declared(["opus"], _served(text, monkeypatch))
    (cfg,) = resolved.configs
    assert cfg.tool_params == {"reasoning_effort": "none"}


async def test_a_re_resolution_replaces_tool_params_with_the_lines_current_ones(monkeypatch):
    """A line that stops needing them stops sending them: the config is replaced whole."""
    previous = DeclaredModels(configs=(_opus(tool_params={"reasoning_effort": "none"}),))
    resolved, facts = await resolve_declared(
        ["opus"],
        _served(_CATALOG_TEXT, monkeypatch),
        previous=previous,
    )
    assert resolved.configs[0].tool_params == {}
    assert [fact.change for fact in facts] == [AliasChange.MODEL]


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


async def test_a_declaration_the_catalog_does_not_carry_leaves_the_others_resolved(
    monkeypatch,
):
    """One misspelled alias is one handle's failure: everything else declared beside it
    resolves, and the message the call naming it raises is carried on the handle."""
    pinned = LLMConfig(name="mine", base_url="https://mine/v1", model="big", api_key_ref="K")
    resolved, _facts = await resolve_declared(
        ["opus", "opuss", pinned],
        _served(_CATALOG_TEXT, monkeypatch),
    )
    assert [cfg.name for cfg in resolved.configs] == ["anthropic-claude-opus-5", "mine"]
    assert resolved.unresolved == {
        "opuss": (
            "direct= names 'opuss', which the paid catalog does not carry — available aliases: opus"
        ),
    }


async def test_a_catalog_that_cannot_be_read_at_all_leaves_only_the_aliases_unresolved(
    monkeypatch,
):
    """A stated config needs no catalog, so it resolves whatever the read did; the
    aliases carry the reason there was nothing to resolve against."""

    def _offline(_name):
        raise ValueError("offline in tests")

    monkeypatch.setattr("llmbroker.broker.presets.bundled_preset_text", lambda _name: None)
    monkeypatch.setattr("llmbroker.broker.presets.fetch_preset_text", _offline)
    pinned = LLMConfig(name="mine", base_url="https://mine/v1", model="big", api_key_ref="K")
    resolved, _facts = await resolve_declared(["opus", pinned], PresetSource())
    assert resolved.configs == (pinned,)
    assert list(resolved.unresolved) == ["opus"]
    assert "could not be read: offline in tests" in resolved.unresolved["opus"]


async def test_a_re_resolution_never_drops_an_alias_that_was_resolving(monkeypatch):
    """The catalog moving out from under a running process says nothing about where a
    resolved alias points, so it keeps answering from the entry it is already on."""
    previous = DeclaredModels(configs=(_opus(),))
    empty = '[[provider]]\nid="anthropic"\nbase_url="https://x/v1"\napi_key_ref="A"\n'
    resolved, facts = await resolve_declared(
        ["opus"],
        _served(empty, monkeypatch),
        previous=previous,
    )
    assert resolved.configs == (_opus(),)
    assert resolved.unresolved == {}
    assert facts == (AliasFact(change=AliasChange.DROPPED, alias="opus", was="claude-opus-4-8"),)


async def test_keeping_a_dropped_alias_does_not_also_keep_a_typo_the_catalog_has_fixed(
    monkeypatch,
):
    """The kept entry is one handle's, not the whole resolution's: everything beside it
    follows the catalog just read, so a handle that never resolved resolves here."""
    previous = DeclaredModels(
        configs=(_opus(name="anthropic-claude-sonnet-5", model="claude-sonnet-5", alias="sonnet"),),
        unresolved=MappingProxyType({"opus": "stale — the catalog did not carry it"}),
    )
    resolved, facts = await resolve_declared(
        ["opus", "sonnet"],
        _served(_CATALOG_TEXT, monkeypatch),
        previous=previous,
    )
    assert [(cfg.alias, cfg.model) for cfg in resolved.configs] == [
        ("opus", "claude-opus-5"),
        ("sonnet", "claude-sonnet-5"),
    ]
    assert resolved.unresolved == {}
    assert [fact.change for fact in facts] == [AliasChange.DROPPED]


async def test_a_kept_alias_keeps_its_key_help_while_the_rest_follows_the_catalog(monkeypatch):
    """The catalog just read no longer names that provider, so it has nothing to say
    about where its key comes from — the entry still answering keeps what resolved it,
    and every other handle takes the help the read just made carries."""
    previous = DeclaredModels(
        configs=(
            _opus(),
            LLMConfig(
                name="openai-gpt-4",
                base_url="https://api.openai.com/v1",
                model="gpt-4",
                api_key_ref="OPENAI_API_KEY",
                alias="gpt",
            ),
        ),
        key_help=MappingProxyType(
            {"ANTHROPIC_API_KEY": "console.anthropic.com", "OPENAI_API_KEY": "the old address"},
        ),
    )
    text = (
        '[[provider]]\nid="anthropic"\nbase_url="https://api.anthropic.com/v2"\n'
        'api_key_ref="ANTHROPIC_API_KEY"\nkey_help="console.anthropic.com"\n'
        '[[provider]]\nid="openai"\nbase_url="https://api.openai.com/v1"\n'
        'api_key_ref="OPENAI_API_KEY"\nkey_help="platform.openai.com"\n'
        '  [[provider.models]]\n  alias="gpt"\n  model="gpt-5"\n'
    )
    resolved, facts = await resolve_declared(
        ["opus", "gpt"],
        _served(text, monkeypatch),
        previous=previous,
    )
    assert [fact.change for fact in facts] == [AliasChange.MODEL, AliasChange.DROPPED]
    assert resolved.key_help == {
        "ANTHROPIC_API_KEY": "console.anthropic.com",
        "OPENAI_API_KEY": "platform.openai.com",
    }


async def test_an_unreadable_catalog_with_nothing_to_say_still_says_it_was_unreadable(
    monkeypatch,
):
    """The wording turns on whether the catalog could be read, never on whether the
    failure carried a message: one with an empty `str()` is not a missing alias."""

    def _offline(_name):
        raise OSError

    monkeypatch.setattr("llmbroker.broker.presets.bundled_preset_text", lambda _name: None)
    monkeypatch.setattr("llmbroker.broker.presets.fetch_preset_text", _offline)
    resolved, _facts = await resolve_declared(["opus"], PresetSource())
    assert resolved.unresolved["opus"] == (
        "direct= names 'opus', and the paid catalog could not be read: OSError"
    )


async def test_a_re_resolution_keeps_reporting_a_handle_that_never_resolved(monkeypatch):
    """A handle with nothing to keep is not what that rule protects: it stays unresolved
    rather than failing the whole re-resolution."""
    previous = DeclaredModels(configs=(_opus(),))
    resolved, _facts = await resolve_declared(
        ["opus", "opuss"],
        _served(_CATALOG_TEXT, monkeypatch),
        previous=previous,
    )
    assert [cfg.name for cfg in resolved.configs] == ["anthropic-claude-opus-5"]
    assert list(resolved.unresolved) == ["opuss"]

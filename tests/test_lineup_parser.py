"""One parser decides whether a lineup file is valid, whoever reads it."""

import tomllib

import pytest

from llmbroker.broker.keys import KeyProbe
from llmbroker.broker.lineup_file import sync_lineup_file
from llmbroker.broker.merge import SyncSource
from llmbroker.standalone.registry import Registry, parse_lineup
from llmbroker.standalone.secrets import DictSecrets

_NEW = (
    '[[llms]]\nname = "gemini"\nbase_url = "https://g/v1"\nmodel = "m"\n'
    'api_key_ref = "GEMINI_API_KEY"\n'
)

_DUPLICATE_NAME = (
    '[[llms]]\nname="a"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="A"\n'
    '[[custom]]\nname="a"\nbase_url="https://y/v1"\nmodel="m"\napi_key_ref="B"\n'
)


async def _sync(text: str, target) -> None:
    """The sync path's own read of the same files the registry reads."""
    await sync_lineup_file(
        target,
        SyncSource(label="freetier", lineup=parse_lineup(tomllib.loads(text)), preset=True),
        probe=KeyProbe(DictSecrets({})),
    )


_DUPLICATE_ALIAS = (
    '[[custom]]\nname="one"\nalias="fast"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="A"\n'
    '[[custom]]\nname="two"\nalias="fast"\nbase_url="https://y/v1"\nmodel="m"\napi_key_ref="B"\n'
)


async def test_registry_and_sync_path_refuse_the_same_duplicate_name(tmp_path):
    target = tmp_path / "llms.toml"
    target.write_text(_DUPLICATE_NAME)
    with pytest.raises(ValueError, match="duplicate name 'a'"):
        await Registry(target).load()
    with pytest.raises(ValueError, match="duplicate name 'a'"):
        await _sync(_NEW, target)
    assert target.read_text() == _DUPLICATE_NAME


async def test_registry_and_sync_path_refuse_the_same_duplicate_alias(tmp_path):
    target = tmp_path / "llms.toml"
    target.write_text(_DUPLICATE_ALIAS)
    with pytest.raises(ValueError, match="duplicate alias 'fast'"):
        await Registry(target).load()
    with pytest.raises(ValueError, match="duplicate alias 'fast'"):
        await _sync(_NEW, target)
    assert target.read_text() == _DUPLICATE_ALIAS


async def test_an_incoming_lineup_is_validated_too(tmp_path):
    target = tmp_path / "llms.toml"
    target.write_text(_NEW)
    with pytest.raises(ValueError, match="duplicate name 'a'"):
        await _sync(_DUPLICATE_NAME, target)
    assert target.read_text() == _NEW


def test_parse_lineup_reads_configs_and_keys_in_file_order():
    data = tomllib.loads(
        '[[llms]]\nname="a"\nbase_url="u"\nmodel="m"\napi_key_ref="A"\n'
        '[[custom]]\nname="c"\nbase_url="u"\nmodel="m"\napi_key_ref="C"\n'
        '[keys.A]\nhelp="a help"\n',
    )
    lineup = parse_lineup(data)
    configs, keys = lineup.configs, lineup.keys
    assert [(c.name, c.custom) for c in configs] == [("a", False), ("c", True)]
    assert keys["A"].help == "a help"


def test_parse_lineup_refuses_a_non_table_entry():
    """Loud, like a bad weight: a curator wrote this by hand, and a silently dropped
    entry is a model missing from the pool with nothing to say so."""
    with pytest.raises(ValueError, match=r"\[\[llms\]\] entry 2 is str, not a table"):
        parse_lineup({"llms": [{"name": "a", "base_url": "u"}, "oops"]})


def test_a_duplicate_alias_entry_name_says_how_to_pin_it():
    """The collision a catalog move makes: two alias entries land on one name, and
    renaming is the one fix that will not stick."""
    data = tomllib.loads(
        '[[custom]]\nname="dup"\nalias="fast"\nbase_url="u"\nmodel="m"\napi_key_ref="A"\n'
        '[[custom]]\nname="dup"\nalias="cheap"\nbase_url="u"\nmodel="m"\napi_key_ref="B"\n',
    )
    with pytest.raises(ValueError, match="drop its 'alias' to pin it instead"):
        parse_lineup(data)


async def test_a_malformed_keys_table_is_refused_not_a_crash(tmp_path):
    """``[keys]`` carries the one thing a human must act on, so a mangled one is
    named rather than dropped — and the writer never reaches it to fail on its own."""
    target = tmp_path / "llms.toml"
    target.write_text(
        'keys = "GROQ_API_KEY"\n'
        '[[llms]]\nname="groq"\nbase_url="https://groq/v1"\nmodel="m"\napi_key_ref="GROQ_API_KEY"\n',
    )
    with pytest.raises(ValueError, match=r"\[keys\] is str, not a table"):
        await Registry(target).load()
    with pytest.raises(ValueError, match=r"\[keys\] is str, not a table"):
        await _sync(_NEW, target)


def test_a_duplicate_plain_name_does_not_mention_aliases():
    with pytest.raises(ValueError, match="duplicate name 'a'") as exc:
        parse_lineup(tomllib.loads(_DUPLICATE_NAME))
    assert "alias" not in str(exc.value)

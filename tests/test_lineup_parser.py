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
    '[[llms]]\nname="a"\nbase_url="https://y/v1"\nmodel="m"\napi_key_ref="B"\n'
)

_DECLARED_SECTION = (
    '[[llms]]\nname="a"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="A"\n'
    '[[custom]]\nname="mine"\nbase_url="https://y/v1"\nmodel="m"\napi_key_ref="B"\n'
)


async def _sync(text: str, target) -> None:
    """The sync path's own read of the same files the registry reads."""
    await sync_lineup_file(
        target,
        SyncSource(label="freetier", lineup=parse_lineup(tomllib.loads(text))),
        probe=KeyProbe(DictSecrets({})),
    )


async def test_registry_and_sync_path_refuse_the_same_duplicate_name(tmp_path):
    target = tmp_path / "llms.toml"
    target.write_text(_DUPLICATE_NAME)
    with pytest.raises(ValueError, match="duplicate name 'a'"):
        await Registry(target).load()
    with pytest.raises(ValueError, match="duplicate name 'a'"):
        await _sync(_NEW, target)
    assert target.read_text() == _DUPLICATE_NAME


async def test_registry_and_sync_path_refuse_the_same_declared_section(tmp_path):
    """Both readers refuse it, and the message says where a named model goes instead."""
    target = tmp_path / "llms.toml"
    target.write_text(_DECLARED_SECTION)
    with pytest.raises(ValueError, match=r"direct=\[\.\.\.\]"):
        await Registry(target).load()
    with pytest.raises(ValueError, match=r"\[\[custom\]\] entries"):
        await _sync(_NEW, target)
    assert target.read_text() == _DECLARED_SECTION


async def test_an_incoming_lineup_is_validated_too(tmp_path):
    target = tmp_path / "llms.toml"
    target.write_text(_NEW)
    with pytest.raises(ValueError, match="duplicate name 'a'"):
        await _sync(_DUPLICATE_NAME, target)
    assert target.read_text() == _NEW


def test_parse_lineup_reads_configs_and_keys_in_file_order():
    data = tomllib.loads(
        '[[llms]]\nname="a"\nbase_url="u"\nmodel="m"\napi_key_ref="A"\n'
        '[[llms]]\nname="c"\nbase_url="u"\nmodel="m"\napi_key_ref="C"\n'
        '[keys.A]\nhelp="a help"\n',
    )
    lineup = parse_lineup(data)
    configs, keys = lineup.configs, lineup.keys
    assert [(c.name, c.from_preset) for c in configs] == [("a", True), ("c", True)]
    assert keys["A"].help == "a help"


def test_parse_lineup_refuses_a_non_table_entry():
    """Loud, like a bad weight: a curator wrote this by hand, and a silently dropped
    entry is a model missing from the pool with nothing to say so."""
    with pytest.raises(ValueError, match=r"\[\[llms\]\] entry 2 is str, not a table"):
        parse_lineup({"llms": [{"name": "a", "base_url": "u"}, "oops"]})


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


def test_an_alias_on_a_stored_entry_is_not_read_back():
    """The keyspace a stored entry has no field for: a leftover key is ignored, not
    turned into an entry that follows the paid catalog."""
    data = tomllib.loads(
        '[[llms]]\nname="a"\nalias="opus"\nbase_url="u"\nmodel="m"\napi_key_ref="A"\n'
    )
    (cfg,) = parse_lineup(data).configs
    assert cfg.alias is None

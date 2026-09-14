"""Per-provider key help: parsing it in the file Registry, and the help a broker reports
for a missing key whatever its registry is made of."""

import asyncio
import logging
import tomllib

import pytest

from llmbroker.broker import presets
from llmbroker.broker.broker import AsyncBroker
from llmbroker.broker.presets import PresetSource
from llmbroker.exceptions import MissingKeyError
from llmbroker.models import KeyInfo, LLMConfig
from llmbroker.protocols.registry import KeyInfoProtocol
from llmbroker.sqlite import Registry as SqliteRegistry
from llmbroker.standalone.registry import Registry, parse_model_list
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import InMemoryStore


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


# ── The preset bundled in the package ────────────────────────────────────────


def test_shipped_freetier_preset_configs_load():
    configs = asyncio.run(Registry("src/llmbroker/presets/freetier.toml").load())
    assert configs
    assert all(c.model and c.api_key_ref and 0.0 < c.weight <= 1.0 for c in configs)


def test_shipped_freetier_preset_key_info_extra_passthrough():
    """Every pooled key carries the onboarding metadata the curation rules require;
    the counts move with each catalog refresh, the passthrough does not."""
    registry = Registry("src/llmbroker/presets/freetier.toml")
    configs = asyncio.run(registry.load())
    info = asyncio.run(registry.key_info())
    assert {c.api_key_ref for c in configs} == set(info)
    assert all(i.help and set(i.extra) == {"effort", "value"} for i in info.values())


# ── The help a broker reports follows the list it follows, whatever its registry ──

_CURATED = """
[[llms]]
name = "p1"
base_url = "https://x/v1"
model = "m"
api_key_ref = "K"

[[llms]]
name = "p2"
base_url = "https://y/v1"
model = "m"
api_key_ref = "K2"

[keys.K]
help = "curated K help"

[keys.K2]
help = "curated K2 help"
"""


def _cache_the_curated_list(home, text=_CURATED):
    path = home / "presets" / "freetier.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _entry(ref, name="p1"):
    return LLMConfig(
        name=name, base_url="https://x/v1", model="m", api_key_ref=ref, from_preset=True
    )


async def _sqlite_with(tmp_path, *entries):
    db = tmp_path / "broker.db"
    await SqliteRegistry(db).mirror(list(entries))
    return f"sqlite://{db}"


@pytest.fixture
def no_help_read(monkeypatch):
    def _refuse(self, name):
        raise AssertionError("the followed list was read")

    monkeypatch.setattr(PresetSource, "text_for_help", _refuse)


async def test_a_sqlite_installation_reports_the_curated_help_for_a_missing_key(
    tmp_path, llmbroker_home, caplog
):
    _cache_the_curated_list(llmbroker_home)
    source = await _sqlite_with(tmp_path, _entry("K"))
    with caplog.at_level(logging.WARNING, logger="llmbroker"):
        async with AsyncBroker(
            source, secrets=DictSecrets({}), store=InMemoryStore(), sync_interval=None
        ) as broker:
            snap = await broker.snapshot()
    assert [(k.api_key_ref, k.help, k.entry_names) for k in snap.missing_keys] == [
        ("K", "curated K help", ("p1",)),
    ]
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == []


async def test_with_no_copy_cached_the_help_comes_from_the_wheel_without_a_warning(
    tmp_path, bundled_presets, caplog
):
    shipped = parse_model_list(tomllib.loads(presets.bundled_preset_text("freetier")))
    ref = shipped.configs[0].api_key_ref
    source = await _sqlite_with(tmp_path, _entry(ref))
    with caplog.at_level(logging.WARNING, logger="llmbroker"):
        async with AsyncBroker(
            source, secrets=DictSecrets({}), store=InMemoryStore(), sync_interval=None
        ) as broker:
            snap = await broker.snapshot()
    assert [k.help for k in snap.missing_keys] == [shipped.keys[ref].help]
    assert shipped.keys[ref].help
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == []


async def test_a_file_backed_installation_keeps_its_own_help_and_reads_no_list(
    llmbroker_home, no_help_read
):
    _cache_the_curated_list(llmbroker_home)
    (llmbroker_home / "model-list.toml").write_text(
        '[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n'
        '[keys.K]\nhelp = "file help"\n',
    )
    async with AsyncBroker(
        secrets=DictSecrets({}), store=InMemoryStore(), sync_interval=None
    ) as broker:
        snap = await broker.snapshot()
    assert [k.help for k in snap.missing_keys] == ["file help"]


class _HostRegistry:
    """A registry the host composes over a shipped one, carrying key help of its own."""

    def __init__(self, inner):
        self._inner = inner

    async def load(self):
        return await self._inner.load()

    async def key_info(self):
        return {"K": KeyInfo(api_key_ref="K", help="host help", extra={})}


async def test_a_registry_implementing_key_info_still_wins(tmp_path, llmbroker_home):
    _cache_the_curated_list(llmbroker_home)
    await _sqlite_with(tmp_path, _entry("K"), _entry("K2", name="p2"))
    async with AsyncBroker(
        _HostRegistry(SqliteRegistry(tmp_path / "broker.db")),
        secrets=DictSecrets({}),
        store=InMemoryStore(),
        sync="freetier",
        sync_interval=None,
    ) as broker:
        snap = await broker.snapshot()
    assert [(k.api_key_ref, k.help) for k in snap.missing_keys] == [
        ("K", "host help"),
        ("K2", "curated K2 help"),
    ]


async def test_an_installation_following_no_list_reports_no_help(tmp_path, llmbroker_home):
    _cache_the_curated_list(llmbroker_home)
    source = await _sqlite_with(tmp_path, _entry("K"))
    async with AsyncBroker(
        source, secrets=DictSecrets({}), store=InMemoryStore(), sync=None, sync_interval=None
    ) as broker:
        snap = await broker.snapshot()
    assert [(k.api_key_ref, k.help) for k in snap.missing_keys] == [("K", "")]


async def test_a_fully_keyed_installation_reads_no_list(tmp_path, llmbroker_home, no_help_read):
    _cache_the_curated_list(llmbroker_home)
    source = await _sqlite_with(tmp_path, _entry("K"))
    async with AsyncBroker(
        source, secrets=DictSecrets({"K": "key"}), store=InMemoryStore(), sync_interval=None
    ) as broker:
        snap = await broker.snapshot()
    assert snap.missing_keys == ()
    assert snap.direct_missing_keys == ()


async def test_a_declared_models_missing_key_gets_the_curated_help_by_the_same_path(
    tmp_path, llmbroker_home
):
    _cache_the_curated_list(llmbroker_home)
    source = await _sqlite_with(tmp_path, _entry("K"))
    mine = LLMConfig(name="mine", base_url="https://mine/v1", model="m", api_key_ref="K2")
    async with AsyncBroker(
        source,
        secrets=DictSecrets({"K": "key"}),
        store=InMemoryStore(),
        sync_interval=None,
        direct=[mine],
    ) as broker:
        snap = await broker.snapshot()
        with pytest.raises(MissingKeyError, match="curated K2 help"):
            await broker.direct(name="mine")
    assert snap.missing_keys == ()
    assert [(k.api_key_ref, k.help, k.entry_names) for k in snap.direct_missing_keys] == [
        ("K2", "curated K2 help", ("mine",)),
    ]


async def test_an_unreadable_cached_list_yields_no_help_and_does_not_fail_the_rebuild(
    tmp_path, llmbroker_home
):
    _cache_the_curated_list(llmbroker_home, text="this is [not toml")
    source = await _sqlite_with(tmp_path, _entry("K"))
    async with AsyncBroker(
        source, secrets=DictSecrets({}), store=InMemoryStore(), sync_interval=None
    ) as broker:
        snap = await broker.snapshot()
    assert [(k.api_key_ref, k.help) for k in snap.missing_keys] == [("K", "")]

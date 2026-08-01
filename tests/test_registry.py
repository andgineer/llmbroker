"""Tests for the file-backed Registry (TOML and JSON) and MutableRegistryProtocol backends."""

import asyncio
import json

import aiosqlite
import pytest

from llmbroker.models import LLMConfig
from llmbroker.mongodb import Registry as MongoRegistry
from llmbroker.postgres import Registry as PostgresRegistry
from llmbroker.sqlite import Registry as SqliteRegistry
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


def test_load_custom_array_flags_and_pool(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text(
        '[[llms]]\nname="m"\nbase_url="https://x/v1"\nmodel="a"\napi_key_ref="K"\n'
        '[[custom]]\nname="c-pooled"\nbase_url="https://y/v1"\nmodel="b"\napi_key_ref="K"\n'
        '[[custom]]\nname="c-direct"\nbase_url="https://z/v1"\nmodel="c"\napi_key_ref="K"\npool=false\n'
    )
    configs = {c.name: c for c in asyncio.run(Registry(f).load())}
    assert (configs["m"].custom, configs["m"].pooled) == (False, True)
    assert (configs["c-pooled"].custom, configs["c-pooled"].pooled) == (True, True)
    assert (configs["c-direct"].custom, configs["c-direct"].pooled) == (True, False)


# ── alias ────────────────────────────────────────────────────────────────────


def test_load_alias_on_custom_entry(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text(
        '[[custom]]\nname="anthropic-claude-opus-4-8"\nalias="opus"\n'
        'base_url="https://a/v1"\nmodel="claude-opus-4-8"\napi_key_ref="K"\npool=false\n'
        '[[custom]]\nname="pinned"\nbase_url="https://b/v1"\nmodel="m"\napi_key_ref="K"\n'
    )
    configs = {c.name: c for c in asyncio.run(Registry(f).load())}
    assert configs["anthropic-claude-opus-4-8"].alias == "opus"
    assert configs["pinned"].alias is None


def test_load_alias_on_llms_entry_is_a_config_error(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text(
        '[[llms]]\nname="pool"\nalias="free"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n'
    )
    with pytest.raises(ValueError, match="aliases belong to"):
        asyncio.run(Registry(f).load())


def test_load_duplicate_aliases_refused(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text(
        '[[custom]]\nname="a"\nalias="opus"\nbase_url="https://a/v1"\nmodel="m1"\napi_key_ref="K"\n'
        '[[custom]]\nname="b"\nalias="opus"\nbase_url="https://b/v1"\nmodel="m2"\napi_key_ref="K"\n'
    )
    with pytest.raises(ValueError, match="duplicate alias"):
        asyncio.run(Registry(f).load())


def test_load_duplicate_names_across_arrays_refused(tmp_path):
    """The shape a catalog refresh can produce: an alias entry renamed onto a
    preset pool entry. A DB sync keys on the name and would lose one of them."""
    f = tmp_path / "llms.toml"
    f.write_text(
        '[[llms]]\nname="google-gemini-2.5-flash"\nbase_url="https://g/v1"'
        '\nmodel="gemini-2.5-flash"\napi_key_ref="K"\n'
        '[[custom]]\nalias="flash-mini"\nname="google-gemini-2.5-flash"'
        '\nmodel="gemini-2.5-flash"\nbase_url="https://g/v1"\napi_key_ref="K"\npool=false\n'
    )
    with pytest.raises(ValueError, match="duplicate name"):
        asyncio.run(Registry(f).load())


def test_llmconfig_alias_metadata_round_trip():
    cfg = LLMConfig(
        name="anthropic-claude-opus-4-8",
        base_url="u",
        model="claude-opus-4-8",
        api_key_ref="K",
        pooled=False,
        custom=True,
        alias="opus",
    )
    metadata = cfg.to_metadata()
    assert metadata == {"pool": False, "custom": True, "alias": "opus"}
    assert (
        LLMConfig.from_metadata(
            name=cfg.name,
            base_url=cfg.base_url,
            model=cfg.model,
            api_key_ref=cfg.api_key_ref,
            metadata=metadata,
        )
        == cfg
    )


# ── SQLite registry tests ─────────────────────────────────────────────────────


def _cfg(name: str, url: str = "https://x/v1") -> LLMConfig:
    return LLMConfig(name=name, base_url=url, model="m", api_key_ref="K")


def test_sqlite_registry_mirror_updates_existing(tmp_path):
    db = str(tmp_path / "b.db")
    reg = SqliteRegistry(db)

    async def run():
        await reg.mirror([_cfg("p1", "https://old/v1")])
        await reg.mirror([_cfg("p1", "https://new/v1")])
        result = {c.name: c for c in await reg.load()}
        assert result["p1"].base_url == "https://new/v1"

    asyncio.run(run())


def test_sqlite_registry_mirror_deletes_absent(tmp_path):
    db = str(tmp_path / "b.db")
    reg = SqliteRegistry(db)

    async def run():
        await reg.mirror([_cfg("p1"), _cfg("p2")])
        await reg.mirror([_cfg("p1")])
        result = {c.name: c for c in await reg.load()}
        assert set(result) == {"p1"}

    asyncio.run(run())


# ── Parametrized backend tests for the MutableRegistryProtocol ───────────────


@pytest.fixture(
    params=["sqlite", "postgres", "mongodb"],
    ids=["sqlite", "postgres", "mongodb"],
)
async def mutable_registry(request, tmp_path_factory, pg_pool, mongo_db):
    param = request.param
    if param == "sqlite":
        db_path = str(tmp_path_factory.mktemp("mreg_sqlite") / "reg.db")
        yield SqliteRegistry(db_path)
    elif param == "postgres":
        yield PostgresRegistry(pg_pool)
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM llmbroker_registry")
    elif param == "mongodb":
        yield MongoRegistry(mongo_db)
        await mongo_db["llmbroker_registry"].delete_many({})


async def test_mutable_mirror_adds(mutable_registry):
    await mutable_registry.mirror([_cfg("llm1")])
    rows = await mutable_registry.load()
    assert any(r.name == "llm1" for r in rows)


async def test_mutable_mirror_updates_existing_fields(mutable_registry):
    await mutable_registry.mirror([_cfg("p1", "https://old/v1")])
    await mutable_registry.mirror([_cfg("p1", "https://new/v1")])
    result = {c.name: c for c in await mutable_registry.load()}
    assert result["p1"].base_url == "https://new/v1"


async def test_mutable_mirror_deletes_absent_entries(mutable_registry):
    await mutable_registry.mirror([_cfg("p1"), _cfg("p2")])
    await mutable_registry.mirror([_cfg("p1")])
    result = {c.name: c for c in await mutable_registry.load()}
    assert set(result) == {"p1"}


async def test_mutable_mirror_empty_list_deletes_everything(mutable_registry):
    await mutable_registry.mirror([_cfg("p1")])
    await mutable_registry.mirror([])
    assert await mutable_registry.load() == []


# ── LLMConfig ⇄ metadata round-trip (model level) ────────────────────────────


def test_llmconfig_metadata_round_trip_with_parallel():
    cfg = LLMConfig(
        name="g",
        base_url="https://x/v1",
        model="m",
        api_key_ref="K",
        parallel=3,
    )
    metadata = cfg.to_metadata()
    restored = LLMConfig.from_metadata(
        name=cfg.name,
        base_url=cfg.base_url,
        model=cfg.model,
        api_key_ref=cfg.api_key_ref,
        metadata=metadata,
    )
    assert restored == cfg


def test_llmconfig_custom_metadata_round_trip():
    cfg = LLMConfig(name="g", base_url="u", model="m", api_key_ref="K", pooled=False, custom=True)
    assert cfg.to_metadata() == {"pool": False, "custom": True}
    restored = LLMConfig.from_metadata(
        name="g", base_url="u", model="m", api_key_ref="K", metadata={"pool": False, "custom": True}
    )
    assert restored == cfg


def test_llmconfig_metadata_round_trip_without_parallel():
    cfg = _cfg("g")
    assert cfg.to_metadata() == {}
    restored = LLMConfig.from_metadata(
        name=cfg.name,
        base_url=cfg.base_url,
        model=cfg.model,
        api_key_ref=cfg.api_key_ref,
        metadata=None,
    )
    assert restored == cfg


# ── parallel round-trip through DB registries ──────────────────────────────


async def test_mutable_registry_parallel_round_trip(mutable_registry):
    cfg = LLMConfig(
        name="p1",
        base_url="https://x/v1",
        model="m",
        api_key_ref="K",
        parallel=3,
    )
    await mutable_registry.mirror([cfg])
    result = {c.name: c for c in await mutable_registry.load()}
    assert result["p1"].parallel == 3


async def test_mutable_registry_alias_round_trip(mutable_registry):
    cfg = LLMConfig(
        name="anthropic-claude-opus-4-8",
        base_url="https://x/v1",
        model="claude-opus-4-8",
        api_key_ref="K",
        pooled=False,
        custom=True,
        alias="opus",
    )
    await mutable_registry.mirror([cfg])
    result = {c.name: c for c in await mutable_registry.load()}
    assert result["anthropic-claude-opus-4-8"].alias == "opus"


async def test_mutable_registry_refuses_duplicate_aliases_on_load(mutable_registry):
    """The store keys on the name, so two names may carry one alias — and a lookup
    by alias would silently resolve to whichever came back first."""

    def _aliased(name: str) -> LLMConfig:
        return LLMConfig(
            name=name,
            base_url="https://x/v1",
            model=name,
            api_key_ref="K",
            pooled=False,
            custom=True,
            alias="opus",
        )

    await mutable_registry.mirror([_aliased("a-1"), _aliased("a-2")])
    with pytest.raises(ValueError, match="duplicate alias"):
        await mutable_registry.load()


async def test_mutable_registry_parallel_none_round_trip(mutable_registry):
    await mutable_registry.mirror([_cfg("p1")])
    result = {c.name: c for c in await mutable_registry.load()}
    assert result["p1"].parallel is None


async def test_mutable_registry_update_preserves_changed_parallel(mutable_registry):
    await mutable_registry.mirror([_cfg("p1")])
    updated = LLMConfig(
        name="p1",
        base_url="https://new/v1",
        model="m",
        api_key_ref="K",
        parallel=1,
    )
    await mutable_registry.mirror([updated])
    result = {c.name: c for c in await mutable_registry.load()}
    assert result["p1"].parallel == 1


# ── Legacy rows with NULL/absent metadata (pre-migration shape) ──────────────


def test_sqlite_registry_legacy_null_metadata_loads_parallel_none(tmp_path):
    db = str(tmp_path / "b.db")
    reg = SqliteRegistry(db)

    async def run():
        await reg.mirror([_cfg("p1")])
        async with aiosqlite.connect(db) as conn:
            await conn.execute("UPDATE llmbroker_registry SET metadata = NULL WHERE name = 'p1'")
            await conn.commit()
        result = {c.name: c for c in await reg.load()}
        assert result["p1"].parallel is None

    asyncio.run(run())


async def test_postgres_registry_legacy_null_metadata_loads_parallel_none(pg_pool):
    reg = PostgresRegistry(pg_pool)
    try:
        await reg.mirror([_cfg("legacy-null-meta")])
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE llmbroker_registry SET metadata = NULL WHERE name = 'legacy-null-meta'",
            )
        result = {c.name: c for c in await reg.load()}
        assert result["legacy-null-meta"].parallel is None
    finally:
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM llmbroker_registry WHERE name = 'legacy-null-meta'")


async def test_mongodb_registry_legacy_doc_without_metadata_loads_parallel_none(mongo_db):
    reg = MongoRegistry(mongo_db)
    try:
        await mongo_db["llmbroker_registry"].insert_one(
            {
                "name": "legacy-doc",
                "base_url": "https://x/v1",
                "model": "m",
                "api_key_ref": "K",
                "user_id": None,
            },
        )
        result = {c.name: c for c in await reg.load()}
        assert result["legacy-doc"].parallel is None
    finally:
        await mongo_db["llmbroker_registry"].delete_many({"name": "legacy-doc"})

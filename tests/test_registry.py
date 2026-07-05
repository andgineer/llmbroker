"""Tests for the file-backed Registry (TOML and JSON) and MutableRegistryProtocol backends."""

import asyncio
import json

import aiosqlite
import llmbroker.mongodb
import llmbroker.postgres
import llmbroker.sqlite
import pytest

from llmbroker.models import LLMConfig
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


# ── SQLite registry per-user scoping tests ────────────────────────────────────


def _cfg(name: str, url: str = "https://x/v1") -> LLMConfig:
    return LLMConfig(name=name, base_url=url, model="m", api_key_ref="K")


def test_sqlite_registry_per_user_row_isolated(tmp_path):
    """Per-user rows are not visible to other users."""
    db = str(tmp_path / "b.db")
    reg = llmbroker.sqlite.Registry(db)

    async def run():
        await reg.add(_cfg("alice-llm"), "alice")
        alice_rows = await reg.load("alice")
        bob_rows = await reg.load("bob")
        assert len(alice_rows) == 1
        assert len(bob_rows) == 0

    asyncio.run(run())


def test_sqlite_registry_same_name_different_users_allowed(tmp_path):
    """Two users can have rows with the same name without conflict."""
    db = str(tmp_path / "b.db")
    reg = llmbroker.sqlite.Registry(db)

    async def run():
        await reg.add(_cfg("llm", "https://a/v1"), "alice")
        await reg.add(_cfg("llm", "https://b/v1"), "bob")
        alice_rows = await reg.load("alice")
        bob_rows = await reg.load("bob")
        assert alice_rows[0].base_url == "https://a/v1"
        assert bob_rows[0].base_url == "https://b/v1"

    asyncio.run(run())


def test_sqlite_registry_duplicate_within_user_rejected(tmp_path):
    """Adding the same name twice for the same user raises ValueError."""
    db = str(tmp_path / "b.db")
    reg = llmbroker.sqlite.Registry(db)

    async def run():
        await reg.add(_cfg("llm"), "alice")
        with pytest.raises(ValueError, match="already exists"):
            await reg.add(_cfg("llm"), "alice")

    asyncio.run(run())


def test_sqlite_registry_load_none_returns_only_unscoped(tmp_path):
    """load(None) returns only NULL-scoped rows and does not bleed named-user rows."""
    db = str(tmp_path / "b.db")
    reg = llmbroker.sqlite.Registry(db)

    async def run():
        await reg.add(_cfg("shared"))
        await reg.add(_cfg("alice-llm"), "alice")
        none_rows = await reg.load()
        alice_rows = await reg.load("alice")
        assert [r.name for r in none_rows] == ["shared"]
        assert [r.name for r in alice_rows] == ["alice-llm"]

    asyncio.run(run())


def test_sqlite_registry_get_existing(tmp_path):
    db = str(tmp_path / "b.db")
    reg = llmbroker.sqlite.Registry(db)

    async def run():
        await reg.add(_cfg("p1", "https://x/v1"))
        result = await reg.get("p1")
        assert result is not None
        assert result.name == "p1"
        assert result.base_url == "https://x/v1"

    asyncio.run(run())


def test_sqlite_registry_get_missing_returns_none(tmp_path):
    db = str(tmp_path / "b.db")
    reg = llmbroker.sqlite.Registry(db)

    async def run():
        assert await reg.get("ghost") is None

    asyncio.run(run())


def test_sqlite_registry_update_missing_raises_key_error(tmp_path):
    db = str(tmp_path / "b.db")
    reg = llmbroker.sqlite.Registry(db)

    async def run():
        with pytest.raises(KeyError):
            await reg.update(_cfg("ghost"))

    asyncio.run(run())


def test_sqlite_registry_remove_missing_raises_key_error(tmp_path):
    db = str(tmp_path / "b.db")
    reg = llmbroker.sqlite.Registry(db)

    async def run():
        with pytest.raises(KeyError):
            await reg.remove("ghost")

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
        yield llmbroker.sqlite.Registry(db_path)
    elif param == "postgres":
        yield llmbroker.postgres.Registry(pg_pool)
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM llmbroker_registry")
    elif param == "mongodb":
        yield llmbroker.mongodb.Registry(mongo_db)
        await mongo_db["llmbroker_registry"].delete_many({})


async def test_mutable_add_and_load(mutable_registry):
    await mutable_registry.add(_cfg("llm1"))
    rows = await mutable_registry.load()
    assert any(r.name == "llm1" for r in rows)


async def test_mutable_load_returns_only_matching_user(mutable_registry):
    await mutable_registry.add(_cfg("alice-llm"), "alice")
    alice = await mutable_registry.load("alice")
    bob = await mutable_registry.load("bob")
    assert len(alice) == 1
    assert len(bob) == 0


async def test_mutable_same_name_different_users_allowed(mutable_registry):
    await mutable_registry.add(_cfg("llm", "https://a/v1"), "alice")
    await mutable_registry.add(_cfg("llm", "https://b/v1"), "bob")
    assert (await mutable_registry.load("alice"))[0].base_url == "https://a/v1"
    assert (await mutable_registry.load("bob"))[0].base_url == "https://b/v1"


async def test_mutable_duplicate_within_user_raises_value_error(mutable_registry):
    await mutable_registry.add(_cfg("llm"), "alice")
    with pytest.raises(ValueError, match="already exists"):
        await mutable_registry.add(_cfg("llm"), "alice")


async def test_mutable_load_none_returns_unscoped_only(mutable_registry):
    await mutable_registry.add(_cfg("shared"))
    await mutable_registry.add(_cfg("alice-llm"), "alice")
    none_rows = await mutable_registry.load()
    assert [r.name for r in none_rows] == ["shared"]


async def test_mutable_get_existing(mutable_registry):
    await mutable_registry.add(_cfg("p1", "https://x/v1"))
    result = await mutable_registry.get("p1")
    assert result is not None
    assert result.base_url == "https://x/v1"


async def test_mutable_get_missing_returns_none(mutable_registry):
    assert await mutable_registry.get("ghost") is None


async def test_mutable_update_changes_fields(mutable_registry):
    await mutable_registry.add(_cfg("p1", "https://old/v1"))
    await mutable_registry.update(_cfg("p1", "https://new/v1"))
    result = await mutable_registry.get("p1")
    assert result is not None
    assert result.base_url == "https://new/v1"


async def test_mutable_update_missing_raises_key_error(mutable_registry):
    with pytest.raises(KeyError):
        await mutable_registry.update(_cfg("ghost"))


async def test_mutable_remove_deletes_entry(mutable_registry):
    await mutable_registry.add(_cfg("p1"))
    await mutable_registry.remove("p1")
    assert await mutable_registry.get("p1") is None


async def test_mutable_remove_missing_raises_key_error(mutable_registry):
    with pytest.raises(KeyError):
        await mutable_registry.remove("ghost")


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
    await mutable_registry.add(cfg)
    result = await mutable_registry.get("p1")
    assert result is not None
    assert result.parallel == 3


async def test_mutable_registry_parallel_none_round_trip(mutable_registry):
    await mutable_registry.add(_cfg("p1"))
    result = await mutable_registry.get("p1")
    assert result is not None
    assert result.parallel is None


async def test_mutable_registry_update_preserves_changed_parallel(mutable_registry):
    await mutable_registry.add(_cfg("p1"))
    updated = LLMConfig(
        name="p1",
        base_url="https://new/v1",
        model="m",
        api_key_ref="K",
        parallel=1,
    )
    await mutable_registry.update(updated)
    result = await mutable_registry.get("p1")
    assert result is not None
    assert result.parallel == 1


# ── Legacy rows with NULL/absent metadata (pre-migration shape) ──────────────


def test_sqlite_registry_legacy_null_metadata_loads_parallel_none(tmp_path):
    db = str(tmp_path / "b.db")
    reg = llmbroker.sqlite.Registry(db)

    async def run():
        await reg.add(_cfg("p1"))
        async with aiosqlite.connect(db) as conn:
            await conn.execute("UPDATE llmbroker_registry SET metadata = NULL WHERE name = 'p1'")
            await conn.commit()
        result = await reg.get("p1")
        assert result is not None
        assert result.parallel is None

    asyncio.run(run())


async def test_postgres_registry_legacy_null_metadata_loads_parallel_none(pg_pool):
    reg = llmbroker.postgres.Registry(pg_pool)
    try:
        await reg.add(_cfg("legacy-null-meta"))
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE llmbroker_registry SET metadata = NULL WHERE name = 'legacy-null-meta'",
            )
        result = await reg.get("legacy-null-meta")
        assert result is not None
        assert result.parallel is None
    finally:
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM llmbroker_registry WHERE name = 'legacy-null-meta'")


async def test_mongodb_registry_legacy_doc_without_metadata_loads_parallel_none(mongo_db):
    reg = llmbroker.mongodb.Registry(mongo_db)
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
        result = await reg.get("legacy-doc")
        assert result is not None
        assert result.parallel is None
    finally:
        await mongo_db["llmbroker_registry"].delete_many({"name": "legacy-doc"})

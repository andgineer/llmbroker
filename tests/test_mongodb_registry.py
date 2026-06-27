"""Integration tests for mongodb.Registry."""

import pytest

pytest.importorskip("motor.motor_asyncio")

from llmbroker.models import LLMConfig
from llmbroker.mongodb import Registry


def _cfg(name: str, url: str = "https://x/v1") -> LLMConfig:
    return LLMConfig(name=name, base_url=url, model="m", api_key_ref="K")


@pytest.fixture(autouse=True)
async def clean(mongo_db):
    yield
    await mongo_db["llmbroker_registry"].delete_many({})


async def test_add_and_load(mongo_db):
    reg = Registry(mongo_db)
    await reg.add(_cfg("llm1"))
    rows = await reg.load()
    assert any(r.name == "llm1" for r in rows)


async def test_load_returns_only_matching_user(mongo_db):
    reg = Registry(mongo_db)
    await reg.add(_cfg("alice-llm"), "alice")
    alice = await reg.load("alice")
    bob = await reg.load("bob")
    assert len(alice) == 1
    assert len(bob) == 0


async def test_same_name_different_users_allowed(mongo_db):
    reg = Registry(mongo_db)
    await reg.add(_cfg("llm", "https://a/v1"), "alice")
    await reg.add(_cfg("llm", "https://b/v1"), "bob")
    assert (await reg.load("alice"))[0].base_url == "https://a/v1"
    assert (await reg.load("bob"))[0].base_url == "https://b/v1"


async def test_duplicate_within_user_raises_value_error(mongo_db):
    reg = Registry(mongo_db)
    await reg.add(_cfg("llm"), "alice")
    with pytest.raises(ValueError, match="already exists"):
        await reg.add(_cfg("llm"), "alice")


async def test_load_none_returns_unscoped_only(mongo_db):
    reg = Registry(mongo_db)
    await reg.add(_cfg("shared"))
    await reg.add(_cfg("alice-llm"), "alice")
    none_rows = await reg.load()
    assert [r.name for r in none_rows] == ["shared"]


async def test_get_existing(mongo_db):
    reg = Registry(mongo_db)
    await reg.add(_cfg("p1", "https://x/v1"))
    result = await reg.get("p1")
    assert result is not None
    assert result.base_url == "https://x/v1"


async def test_get_missing_returns_none(mongo_db):
    reg = Registry(mongo_db)
    assert await reg.get("ghost") is None


async def test_update_changes_fields(mongo_db):
    reg = Registry(mongo_db)
    await reg.add(_cfg("p1", "https://old/v1"))
    await reg.update(_cfg("p1", "https://new/v1"))
    result = await reg.get("p1")
    assert result is not None
    assert result.base_url == "https://new/v1"


async def test_update_missing_raises_key_error(mongo_db):
    reg = Registry(mongo_db)
    with pytest.raises(KeyError):
        await reg.update(_cfg("ghost"))


async def test_remove_deletes_entry(mongo_db):
    reg = Registry(mongo_db)
    await reg.add(_cfg("p1"))
    await reg.remove("p1")
    assert await reg.get("p1") is None


async def test_remove_missing_raises_key_error(mongo_db):
    reg = Registry(mongo_db)
    with pytest.raises(KeyError):
        await reg.remove("ghost")


async def test_aclose_is_noop(mongo_db):
    reg = Registry(mongo_db)
    await reg.aclose()

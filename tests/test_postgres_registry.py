"""Integration tests for postgres.Registry."""

import pytest

asyncpg = pytest.importorskip("asyncpg")

from llmbroker.models import LLMConfig
from llmbroker.postgres import Registry


def _cfg(name: str, url: str = "https://x/v1") -> LLMConfig:
    return LLMConfig(name=name, base_url=url, model="m", api_key_ref="K")


@pytest.fixture(autouse=True)
async def clean(pg_pool):
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM llmbroker_registry")


async def test_add_and_load(pg_pool):
    reg = Registry(pg_pool)
    await reg.add(_cfg("llm1"))
    rows = await reg.load()
    assert any(r.name == "llm1" for r in rows)


async def test_load_returns_only_matching_user(pg_pool):
    reg = Registry(pg_pool)
    await reg.add(_cfg("alice-llm"), "alice")
    alice = await reg.load("alice")
    bob = await reg.load("bob")
    assert len(alice) == 1
    assert len(bob) == 0


async def test_same_name_different_users_allowed(pg_pool):
    reg = Registry(pg_pool)
    await reg.add(_cfg("llm", "https://a/v1"), "alice")
    await reg.add(_cfg("llm", "https://b/v1"), "bob")
    assert (await reg.load("alice"))[0].base_url == "https://a/v1"
    assert (await reg.load("bob"))[0].base_url == "https://b/v1"


async def test_duplicate_within_user_raises_value_error(pg_pool):
    reg = Registry(pg_pool)
    await reg.add(_cfg("llm"), "alice")
    with pytest.raises(ValueError, match="already exists"):
        await reg.add(_cfg("llm"), "alice")


async def test_load_none_returns_unscoped_only(pg_pool):
    reg = Registry(pg_pool)
    await reg.add(_cfg("shared"))
    await reg.add(_cfg("alice-llm"), "alice")
    none_rows = await reg.load()
    assert [r.name for r in none_rows] == ["shared"]


async def test_get_existing(pg_pool):
    reg = Registry(pg_pool)
    await reg.add(_cfg("p1", "https://x/v1"))
    result = await reg.get("p1")
    assert result is not None
    assert result.base_url == "https://x/v1"


async def test_get_missing_returns_none(pg_pool):
    reg = Registry(pg_pool)
    assert await reg.get("ghost") is None


async def test_update_changes_fields(pg_pool):
    reg = Registry(pg_pool)
    await reg.add(_cfg("p1", "https://old/v1"))
    await reg.update(_cfg("p1", "https://new/v1"))
    result = await reg.get("p1")
    assert result is not None
    assert result.base_url == "https://new/v1"


async def test_update_missing_raises_key_error(pg_pool):
    reg = Registry(pg_pool)
    with pytest.raises(KeyError):
        await reg.update(_cfg("ghost"))


async def test_remove_deletes_entry(pg_pool):
    reg = Registry(pg_pool)
    await reg.add(_cfg("p1"))
    await reg.remove("p1")
    assert await reg.get("p1") is None


async def test_remove_missing_raises_key_error(pg_pool):
    reg = Registry(pg_pool)
    with pytest.raises(KeyError):
        await reg.remove("ghost")


async def test_aclose_is_noop(pg_pool):
    reg = Registry(pg_pool)
    await reg.aclose()

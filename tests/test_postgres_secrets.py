"""Integration tests for postgres.Secrets."""

import pytest

asyncpg = pytest.importorskip("asyncpg")

from llmbroker.exceptions import UserScopeError
from llmbroker.postgres import Secrets


@pytest.fixture(autouse=True)
async def clean(pg_pool):
    yield
    async with pg_pool.acquire() as conn:
        await conn.execute("DELETE FROM llmbroker_secrets")


async def test_set_and_resolve(pg_pool):
    s = Secrets(pg_pool)
    await s.set("K", "secret")
    assert await s.resolve("K") == "secret"


async def test_set_upserts(pg_pool):
    s = Secrets(pg_pool)
    await s.set("K", "v1")
    await s.set("K", "v2")
    assert await s.resolve("K") == "v2"


async def test_resolve_missing_raises_key_error(pg_pool):
    s = Secrets(pg_pool)
    with pytest.raises(KeyError):
        await s.resolve("MISSING")


async def test_two_users_isolated(pg_pool):
    s = Secrets(pg_pool)
    await s.set("K", "alice-val", "alice")
    await s.set("K", "bob-val", "bob")
    assert await s.resolve("K", "alice") == "alice-val"
    assert await s.resolve("K", "bob") == "bob-val"


async def test_user_id_none_resolves_unscoped(pg_pool):
    s = Secrets(pg_pool)
    await s.set("K", "global-val")
    assert await s.resolve("K") == "global-val"


async def test_missing_per_user_raises_key_error(pg_pool):
    s = Secrets(pg_pool)
    await s.set("K", "global-val")
    with pytest.raises(KeyError):
        await s.resolve("K", "alice")


async def test_require_user_id_none_raises_on_resolve(pg_pool):
    s = Secrets(pg_pool, require_user_id=True)
    with pytest.raises(UserScopeError):
        await s.resolve("K", None)


async def test_require_user_id_none_raises_on_set(pg_pool):
    s = Secrets(pg_pool, require_user_id=True)
    with pytest.raises(UserScopeError):
        await s.set("K", "v", None)


async def test_require_user_id_resolves_with_user(pg_pool):
    s = Secrets(pg_pool, require_user_id=True)
    await s.set("K", "val", "alice")
    assert await s.resolve("K", "alice") == "val"


async def test_aclose_is_noop(pg_pool):
    s = Secrets(pg_pool)
    await s.aclose()

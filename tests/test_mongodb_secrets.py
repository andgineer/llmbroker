"""Integration tests for mongodb.Secrets."""

import pytest

pytest.importorskip("motor.motor_asyncio")

from llmbroker.exceptions import UserScopeError
from llmbroker.mongodb import Secrets


@pytest.fixture(autouse=True)
async def clean(mongo_db):
    yield
    await mongo_db["llmbroker_secrets"].delete_many({})


async def test_set_and_resolve(mongo_db):
    s = Secrets(mongo_db)
    await s.set("K", "secret")
    assert await s.resolve("K") == "secret"


async def test_set_upserts(mongo_db):
    s = Secrets(mongo_db)
    await s.set("K", "v1")
    await s.set("K", "v2")
    assert await s.resolve("K") == "v2"


async def test_resolve_missing_raises_key_error(mongo_db):
    s = Secrets(mongo_db)
    with pytest.raises(KeyError):
        await s.resolve("MISSING")


async def test_two_users_isolated(mongo_db):
    s = Secrets(mongo_db)
    await s.set("K", "alice-val", "alice")
    await s.set("K", "bob-val", "bob")
    assert await s.resolve("K", "alice") == "alice-val"
    assert await s.resolve("K", "bob") == "bob-val"


async def test_user_id_none_resolves_unscoped(mongo_db):
    s = Secrets(mongo_db)
    await s.set("K", "global-val")
    assert await s.resolve("K") == "global-val"


async def test_missing_per_user_raises_key_error(mongo_db):
    s = Secrets(mongo_db)
    await s.set("K", "global-val")
    with pytest.raises(KeyError):
        await s.resolve("K", "alice")


async def test_require_user_id_none_raises_on_resolve(mongo_db):
    s = Secrets(mongo_db, require_user_id=True)
    with pytest.raises(UserScopeError):
        await s.resolve("K", None)


async def test_require_user_id_none_raises_on_set(mongo_db):
    s = Secrets(mongo_db, require_user_id=True)
    with pytest.raises(UserScopeError):
        await s.set("K", "v", None)


async def test_require_user_id_resolves_with_user(mongo_db):
    s = Secrets(mongo_db, require_user_id=True)
    await s.set("K", "val", "alice")
    assert await s.resolve("K", "alice") == "val"


async def test_aclose_is_noop(mongo_db):
    s = Secrets(mongo_db)
    await s.aclose()

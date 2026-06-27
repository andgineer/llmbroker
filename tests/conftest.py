"""Session fixtures for postgres and mongodb integration tests."""

import pytest


@pytest.fixture(scope="session")
async def pg_pool():
    asyncpg = pytest.importorskip("asyncpg")
    try:
        pool = await asyncpg.create_pool("postgresql://localhost/llmbroker_test")
    except Exception:
        pytest.skip("postgres not available")
    yield pool
    await pool.close()


@pytest.fixture(scope="session")
async def mongo_db():
    motor_asyncio = pytest.importorskip("motor.motor_asyncio")
    try:
        client = motor_asyncio.AsyncIOMotorClient(
            "mongodb://localhost:27017", serverSelectionTimeoutMS=2000
        )
        await client.server_info()
    except Exception:
        pytest.skip("mongodb not available")
    yield client["llmbroker_test"]
    client.close()

"""Tests for the `stack=` constructor sugar on AsyncBroker/Broker."""

import asyncio
from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import llmbroker.mongodb
import llmbroker.postgres
import llmbroker.redis
import llmbroker.sqlite
import pytest

from llmbroker.broker import AsyncBroker
from llmbroker.models import Call, CallStatus, LLMConfig, LLMState, LifecyclePhase
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.sync import Broker


def _cfg(name: str = "llm1", api_key_ref: str = "KEY") -> LLMConfig:
    return LLMConfig(name=name, base_url="https://x/v1", model="m", api_key_ref=api_key_ref)


async def test_sqlite_stack_wires_all_four_ports(tmp_path):
    db_path = str(tmp_path / "broker.db")
    stack = llmbroker.sqlite.Stack(db_path)

    await stack.registry.add(_cfg())
    await stack.secrets.set("KEY", "secret-value")
    assert await stack.secrets.resolve("KEY") == "secret-value"

    cooldown_until = datetime.now(UTC) + timedelta(hours=1)
    async with AsyncBroker(stack=stack) as broker:
        assert await broker.count() == 1
        assert broker._pool._resolved_keys.get("llm1") == "secret-value"
        await stack.state_store.write(
            "llm1",
            LLMState(phase=LifecyclePhase.COOLING, cooldown_until=cooldown_until),
        )

    state = await stack.state_store.read()
    assert state["llm1"].phase is LifecyclePhase.COOLING


async def test_stack_with_explicit_secrets_override(tmp_path):
    db_path = str(tmp_path / "broker.db")
    stack = llmbroker.sqlite.Stack(db_path)
    await stack.registry.add(_cfg())
    await stack.secrets.set("KEY", "from-stack")
    override_secrets = DictSecrets({"KEY": "from-override"})

    async with AsyncBroker(stack=stack, secrets=override_secrets) as broker:
        assert broker._pool._resolved_keys.get("llm1") == "from-override"


async def test_stack_with_state_store_none_disables_it(tmp_path):
    db_path = str(tmp_path / "broker.db")
    stack = llmbroker.sqlite.Stack(db_path)
    await stack.registry.add(_cfg())

    async with AsyncBroker(stack=stack, state_store=None) as broker:
        assert broker._state_store is None


async def test_stack_with_explicit_state_store_override(tmp_path):
    db_path = str(tmp_path / "broker.db")
    stack = llmbroker.sqlite.Stack(db_path)
    await stack.registry.add(_cfg())
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    override_state_store = llmbroker.redis.StateStore(redis_client)

    async with AsyncBroker(stack=stack, state_store=override_state_store) as broker:
        assert broker._state_store is override_state_store


async def test_no_registry_and_no_stack_raises():
    with pytest.raises(ValueError, match="requires either"):
        AsyncBroker()


async def test_bare_path_shortcut_still_works(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text('[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n')

    async with AsyncBroker(str(f)) as broker:
        assert await broker.count() == 1


@pytest.mark.docker
async def test_postgres_stack_wires_all_four_ports(pg_pool):
    stack = llmbroker.postgres.Stack(pg_pool)
    try:
        await stack.registry.add(_cfg())
        await stack.secrets.set("KEY", "secret-value")

        async with AsyncBroker(stack=stack) as broker:
            assert await broker.count() == 1
            assert broker._pool._resolved_keys.get("llm1") == "secret-value"
            await broker._telemetry.record(
                Call(
                    id="c1",
                    llm_name="llm1",
                    operation=None,
                    trace_id=None,
                    status=CallStatus.OK,
                    http_status=200,
                    latency_ms=10,
                ),
            )
        calls = await stack.telemetry.calls(limit=10)
        assert len(calls) == 1
    finally:
        async with pg_pool.acquire() as conn:
            await conn.execute("DELETE FROM llmbroker_registry")
            await conn.execute("DELETE FROM llmbroker_secrets")
            await conn.execute("DELETE FROM llmbroker_calls")
            await conn.execute("DELETE FROM llmbroker_state")
            await conn.execute("DELETE FROM llmbroker_summaries")


@pytest.mark.docker
async def test_mongodb_stack_wires_all_four_ports(mongo_db):
    stack = llmbroker.mongodb.Stack(mongo_db)
    try:
        await stack.registry.add(_cfg())
        await stack.secrets.set("KEY", "secret-value")

        async with AsyncBroker(stack=stack) as broker:
            assert await broker.count() == 1
            assert broker._pool._resolved_keys.get("llm1") == "secret-value"
    finally:
        for coll in (
            "llmbroker_registry",
            "llmbroker_secrets",
            "llmbroker_calls",
            "llmbroker_state",
            "llmbroker_summaries",
        ):
            await mongo_db[coll].delete_many({})


def test_sync_broker_stack_wires_all_four_ports(tmp_path):
    db_path = str(tmp_path / "broker.db")
    stack = llmbroker.sqlite.Stack(db_path)

    async def _seed():
        await stack.registry.add(_cfg())
        await stack.secrets.set("KEY", "secret-value")

    asyncio.run(_seed())

    with Broker(stack=stack) as broker:
        assert broker.count() == 1


def test_sync_broker_stack_with_state_store_override(tmp_path):
    db_path = str(tmp_path / "broker.db")
    stack = llmbroker.sqlite.Stack(db_path)

    async def _seed():
        await stack.registry.add(_cfg())
        await stack.secrets.set("KEY", "secret-value")

    asyncio.run(_seed())

    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    override_state_store = llmbroker.redis.StateStore(redis_client)
    with Broker(stack=stack, state_store=override_state_store) as broker:
        assert broker._async._state_store is override_state_store


def test_sync_broker_no_registry_and_no_stack_raises():
    with pytest.raises(ValueError, match="requires either"):
        Broker()

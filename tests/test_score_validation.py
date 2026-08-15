"""Quality scores are validated at both public entry points — the Wilson bound
the optimizer derives is only defined on [0, 1].
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, patch

from llmbroker.broker.broker import AsyncBroker
from llmbroker.standalone.registry import Registry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.sqlite import Store as SqliteStore
from llmbroker.standalone.store import InMemoryStore
from llmbroker.sync import Broker

_PATCH = "llmbroker.broker.router.call_provider"


def _registry(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text('[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n')
    return Registry(f)


def _async_broker(tmp_path, store=None) -> AsyncBroker:
    return AsyncBroker(
        registry=_registry(tmp_path),
        secrets=DictSecrets({"K": "test"}),
        store=store if store is not None else InMemoryStore(),
        sync=None,
    )


@pytest.mark.parametrize("score", [-0.1, 1.1, float("nan")])
def test_broker_record_quality_rejects_out_of_range(tmp_path, score):
    """The score is checked before the key is resolved, so a bad one is a ValueError
    rather than an UnknownCallError about a call that was never looked for."""

    async def run():
        async with _async_broker(tmp_path) as broker:
            with pytest.raises(ValueError, match=r"\[0.0, 1.0\]"):
                await broker.record_quality(score, call_id="never-looked-up")

    asyncio.run(run())


@pytest.mark.parametrize("score", [0.0, 1.0, 0.5])
def test_broker_record_quality_accepts_the_boundaries(tmp_path, score):
    async def run():
        async with _async_broker(tmp_path, SqliteStore(str(tmp_path / "b.db"))) as broker:
            with patch(_PATCH, new=AsyncMock(return_value=("ok", None, None))):
                result = await broker.ask("hi")
            await broker.record_quality(score, call_id=result.call_id)

    asyncio.run(run())


@pytest.mark.parametrize("score", [-0.1, 1.1])
def test_result_record_quality_rejects_out_of_range(tmp_path, score):
    async def run():
        async with _async_broker(tmp_path) as broker:
            with patch(_PATCH, new=AsyncMock(return_value=("ok", None, None))):
                result = await broker.ask("hi")
            with pytest.raises(ValueError, match=r"\[0.0, 1.0\]"):
                await result.record_quality(score)

    asyncio.run(run())


@pytest.mark.parametrize("score", [0.0, 1.0])
def test_result_record_quality_accepts_the_boundaries(tmp_path, score):
    async def run():
        async with _async_broker(tmp_path) as broker:
            with patch(_PATCH, new=AsyncMock(return_value=("ok", None, None))):
                result = await broker.ask("hi")
            await result.record_quality(score)

    asyncio.run(run())


def test_sync_broker_record_quality_inherits_the_check(tmp_path):
    broker = Broker(
        registry=_registry(tmp_path),
        secrets=DictSecrets({"K": "test"}),
        store=InMemoryStore(),
        sync=None,
    )
    try:
        with pytest.raises(ValueError, match=r"\[0.0, 1.0\]"):
            broker.record_quality(1.5, call_id="never-looked-up")
    finally:
        broker.close()


def test_sync_result_record_quality_inherits_the_check(tmp_path):
    broker = Broker(
        registry=_registry(tmp_path),
        secrets=DictSecrets({"K": "test"}),
        store=InMemoryStore(),
        sync=None,
    )
    try:
        with patch(_PATCH, new=AsyncMock(return_value=("ok", None, None))):
            result = broker.ask("hi")
        with pytest.raises(ValueError, match=r"\[0.0, 1.0\]"):
            result.record_quality(-1.0)
    finally:
        broker.close()

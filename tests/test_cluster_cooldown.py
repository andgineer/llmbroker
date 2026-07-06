"""Cluster shared-cooldown e2e (mission #6): a cooldown journaled by one broker
propagates to a sibling broker over the same sqlite file on its next rebuild —
gated by key identity for quota failures (429/401/403), unconditional for 5xx.
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from llmbroker.broker.broker import AsyncBroker
from llmbroker.exceptions import NoLLMAvailableError
from llmbroker.models import LLMConfig
from llmbroker.sqlite import Registry as SqliteRegistry
from llmbroker.standalone.secrets import DictSecrets

_PATCH = "llmbroker.broker.router.call_provider"


def _http_status_error(status: int, retry_after: str | None = None) -> httpx.HTTPStatusError:
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {"Retry-After": retry_after} if retry_after is not None else {}
    resp.text = f"HTTP {status}"
    return httpx.HTTPStatusError("err", request=MagicMock(), response=resp)


async def _seed(db: str) -> None:
    await SqliteRegistry(db).mirror(
        [LLMConfig(name="p1", base_url="https://x/v1", model="m", api_key_ref="KEY")],
    )


def test_cooldown_propagates_across_brokers_when_key_matches(tmp_path):
    async def run():
        db = str(tmp_path / "b.db")
        await _seed(db)

        async with AsyncBroker(db, secrets=DictSecrets({"KEY": "shared"})) as broker_a:
            with patch(_PATCH, new=AsyncMock(side_effect=_http_status_error(429, "120"))):
                with pytest.raises(NoLLMAvailableError):
                    await broker_a.chat([{"role": "user", "content": "hi"}], wait=0)

        async with AsyncBroker(db, secrets=DictSecrets({"KEY": "shared"})) as broker_b:
            await broker_b._learning_hook.maybe_rebuild(force=True)
            snap = await broker_b.snapshot()
            assert snap["p1"].cooldown_until is not None
            assert snap["p1"].cooldown_until > datetime.now(UTC)

    asyncio.run(run())


def test_cooldown_does_not_cross_brokers_when_key_differs_on_quota_failure(tmp_path):
    async def run():
        db = str(tmp_path / "b.db")
        await _seed(db)

        async with AsyncBroker(db, secrets=DictSecrets({"KEY": "key-a"})) as broker_a:
            with patch(_PATCH, new=AsyncMock(side_effect=_http_status_error(429, "120"))):
                with pytest.raises(NoLLMAvailableError):
                    await broker_a.chat([{"role": "user", "content": "hi"}], wait=0)

        async with AsyncBroker(db, secrets=DictSecrets({"KEY": "key-b"})) as broker_b:
            await broker_b._learning_hook.maybe_rebuild(force=True)
            snap = await broker_b.snapshot()
            assert snap["p1"].cooldown_until is None

    asyncio.run(run())


def test_5xx_cooldown_crosses_brokers_regardless_of_key(tmp_path):
    async def run():
        db = str(tmp_path / "b.db")
        await _seed(db)

        async with AsyncBroker(db, secrets=DictSecrets({"KEY": "key-a"})) as broker_a:
            with patch(_PATCH, new=AsyncMock(side_effect=_http_status_error(500))):
                with pytest.raises(NoLLMAvailableError):
                    await broker_a.chat([{"role": "user", "content": "hi"}], wait=0)

        async with AsyncBroker(db, secrets=DictSecrets({"KEY": "key-b"})) as broker_b:
            await broker_b._learning_hook.maybe_rebuild(force=True)
            snap = await broker_b.snapshot()
            assert snap["p1"].cooldown_until is not None

    asyncio.run(run())

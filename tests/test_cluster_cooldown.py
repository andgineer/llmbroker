"""Cluster shared-cooldown e2e (mission #6): a cooldown journaled by one broker
propagates to a sibling broker over the same store on its next rebuild — gated by
key identity for quota failures (429/401/403), unconditional for 5xx. Run over
every persistent backend, since the propagation is entirely journal-mediated.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from llmbroker.broker.broker import AsyncBroker
from llmbroker.exceptions import NoLLMAvailableError
from llmbroker.models import LLMConfig
from llmbroker.standalone.secrets import DictSecrets

_PATCH = "llmbroker.broker.router.call_provider"


def _http_status_error(status: int, retry_after: str | None = None) -> httpx.HTTPStatusError:
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {"Retry-After": retry_after} if retry_after is not None else {}
    resp.text = f"HTTP {status}"
    return httpx.HTTPStatusError("err", request=MagicMock(), response=resp)


async def _seed(stack) -> None:
    await stack.registry.mirror(
        [LLMConfig(name="p1", base_url="https://x/v1", model="m", api_key_ref="KEY")],
    )


def _broker(stack, key: str) -> AsyncBroker:
    """A broker over the stack's shared registry/store with its own key value —
    the whole point is two nodes that differ only in the key they resolved."""
    return AsyncBroker(
        stack.registry, secrets=DictSecrets({"KEY": key}), store=stack.store, sync=None
    )


async def _fail_once(broker: AsyncBroker, error: httpx.HTTPStatusError) -> None:
    with patch(_PATCH, new=AsyncMock(side_effect=error)):
        with pytest.raises(NoLLMAvailableError):
            await broker.chat([{"role": "user", "content": "hi"}], wait=0)


async def _peer_cooldown(stack, key: str) -> datetime | None:
    async with _broker(stack, key) as peer:
        await peer._learner.maybe_rebuild(force=True)
        return (await peer.snapshot())["p1"].cooldown_until


async def test_cooldown_propagates_across_brokers_when_key_matches(persistent_stack):
    await _seed(persistent_stack)
    async with _broker(persistent_stack, "shared") as broker_a:
        await _fail_once(broker_a, _http_status_error(429, "120"))

    cooldown = await _peer_cooldown(persistent_stack, "shared")
    assert cooldown is not None
    assert cooldown > datetime.now(UTC)


async def test_cooldown_does_not_cross_brokers_when_key_differs_on_quota_failure(persistent_stack):
    await _seed(persistent_stack)
    async with _broker(persistent_stack, "key-a") as broker_a:
        await _fail_once(broker_a, _http_status_error(429, "120"))

    assert await _peer_cooldown(persistent_stack, "key-b") is None


async def test_5xx_cooldown_crosses_brokers_regardless_of_key(persistent_stack):
    await _seed(persistent_stack)
    async with _broker(persistent_stack, "key-a") as broker_a:
        await _fail_once(broker_a, _http_status_error(500))

    assert await _peer_cooldown(persistent_stack, "key-b") is not None

"""Invariant 11 end to end: what a process learns about availability stays in that
process. Two brokers over one shared registry and store, and a 429 met by one of
them withdraws the model there and nowhere else — the accepted cost of
``decisions.md#availability-is-not-shared``, asserted rather than left to a reader.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from llmbroker.broker.broker import AsyncBroker
from llmbroker.exceptions import NoLLMAvailableError
from llmbroker.models import LifecyclePhase, LLMConfig
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


def _broker(stack, key: str = "shared") -> AsyncBroker:
    return AsyncBroker(
        stack.registry, secrets=DictSecrets({"KEY": key}), store=stack.store, sync=None
    )


async def _fail_once(broker: AsyncBroker, error: httpx.HTTPStatusError) -> None:
    with patch(_PATCH, new=AsyncMock(side_effect=error)):
        with pytest.raises(NoLLMAvailableError):
            await broker.chat([{"role": "user", "content": "hi"}], wait=0)


async def test_a_cooldown_one_broker_met_does_not_withdraw_the_model_on_its_peer(persistent_stack):
    """The 429 is journaled — it is what quality and the admin read is derived from —
    but no peer reads availability back off that row."""
    await _seed(persistent_stack)
    async with _broker(persistent_stack) as broker_a:
        await _fail_once(broker_a, _http_status_error(429, "120"))
        assert (await broker_a.snapshot())["p1"].cooldown_until is not None

    async with _broker(persistent_stack) as peer:
        await peer.rebuild()
        assert (await peer.snapshot())["p1"].cooldown_until is None
        assert peer._pool.state("p1").phase is LifecyclePhase.AVAILABLE


async def test_a_5xx_cooldown_does_not_cross_either(persistent_stack):
    """A provider-side failure is no more shared than a quota one: the rule is about
    where a finding lives, not about whose fault it was."""
    await _seed(persistent_stack)
    async with _broker(persistent_stack) as broker_a:
        await _fail_once(broker_a, _http_status_error(500))

    async with _broker(persistent_stack) as peer:
        await peer.rebuild()
        assert (await peer.snapshot())["p1"].cooldown_until is None


async def test_the_peer_learns_it_on_its_own_first_failing_call(persistent_stack):
    """The accepted cost, in full: one wasted call per process, then the peer holds
    the same verdict it would have inherited."""
    await _seed(persistent_stack)
    async with _broker(persistent_stack) as broker_a:
        await _fail_once(broker_a, _http_status_error(429, "120"))

    async with _broker(persistent_stack) as peer:
        await _fail_once(peer, _http_status_error(429, "120"))
        assert (await peer.snapshot())["p1"].cooldown_until is not None


async def test_a_dead_key_one_broker_met_leaves_its_peer_routing(persistent_stack):
    """A 401 withdraws the ref in the ring that paid. A peer whose own key still
    works must not lose the model over a row it did not write."""
    await _seed(persistent_stack)
    async with _broker(persistent_stack, "revoked") as broker_a:
        await _fail_once(broker_a, _http_status_error(401))
        assert await broker_a._shared_ring.resolve("KEY") is None

    async with _broker(persistent_stack, "still-valid") as peer:
        await peer.rebuild()
        assert await peer._shared_ring.resolve("KEY") == "still-valid"
        with patch(_PATCH, new=AsyncMock(return_value=("ok", None, None))):
            assert (await peer.ask("hi")).text == "ok"

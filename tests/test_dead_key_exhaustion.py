"""Regression test for a confirmed dead-key bug, and the withdrawal's lifetime.

The bug: one dead-key model in the pool used to hang ``ask()`` forever under the
default ``wait=None`` — nothing was ever going to notify the waiter.
The lifetime: the ref stays unpayable in the ring that paid until the next
rebuild, which is what picks up a replaced key; until then no call retries it.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from llmbroker.broker.broker import AsyncBroker
from llmbroker.exceptions import NoLLMAvailableError
from llmbroker.standalone.registry import Registry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import FileStore

_PATCH = "llmbroker.broker.router.call_provider"


def _registry(tmp_path, name="p1"):
    f = tmp_path / "llms.toml"
    f.write_text(f'[[llms]]\nname="{name}"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n')
    return Registry(f)


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {}
    resp.text = f"HTTP {status}"
    return httpx.HTTPStatusError("err", request=MagicMock(), response=resp)


def test_single_dead_key_raises_instead_of_hanging(tmp_path):
    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path),
            secrets=DictSecrets({"K": "dead-key"}),
            store=FileStore(tmp_path / "store"),
            sync=None,
        ) as broker:
            with patch(_PATCH, new=AsyncMock(side_effect=_http_status_error(401))):
                with pytest.raises(NoLLMAvailableError):
                    await asyncio.wait_for(broker.ask("hi"), timeout=5)

    asyncio.run(run())


def test_a_dead_key_is_never_re_attempted_between_rebuilds(tmp_path):
    """The drop is this process's own finding and holds until the pool is rebuilt —
    so a pool whose only key is dead costs one wasted call, not one per call."""

    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path),
            secrets=DictSecrets({"K": "dead-key"}),
            store=FileStore(tmp_path / "store"),
            sync=None,
        ) as broker:
            call_provider = AsyncMock(side_effect=_http_status_error(401))
            with patch(_PATCH, new=call_provider):
                for _ in range(3):
                    with pytest.raises(NoLLMAvailableError):
                        await asyncio.wait_for(broker.ask("hi"), timeout=5)
            assert call_provider.call_count == 1
            assert await broker._shared_ring.resolve("K") is None

    asyncio.run(run())


def test_a_rebuild_that_finds_the_same_dead_value_hands_nothing_back(tmp_path):
    """The exhaustion trigger rebuilds on the very call that met the 401, so a
    rebuild that re-read the same value must not re-arm it — that is the retry storm
    the withdrawal exists to stop."""

    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path),
            secrets=DictSecrets({"K": "dead-key"}),
            store=FileStore(tmp_path / "store"),
            sync=None,
        ) as broker:
            with patch(_PATCH, new=AsyncMock(side_effect=_http_status_error(401))):
                with pytest.raises(NoLLMAvailableError):
                    await asyncio.wait_for(broker.ask("hi"), timeout=5)
            assert await broker._shared_ring.resolve("K") is None

            await broker.rebuild()
            assert await broker._shared_ring.resolve("K") is None

    asyncio.run(run())


def test_replacing_secret_revives_model(tmp_path):
    async def run():
        secrets = DictSecrets({"K": "dead-key"})
        async with AsyncBroker(
            registry=_registry(tmp_path),
            secrets=secrets,
            store=FileStore(tmp_path / "store"),
            sync=None,
        ) as broker:
            with patch(_PATCH, new=AsyncMock(side_effect=_http_status_error(401))):
                with pytest.raises(NoLLMAvailableError):
                    await asyncio.wait_for(broker.ask("hi"), timeout=5)
            assert await broker._shared_ring.resolve("K") is None

            secrets._mapping["K"] = "fresh-key"  # noqa: SLF001 - test double, direct mutation
            await broker.rebuild()

            assert (await broker.snapshot())["p1"].has_key is True
            assert await broker._shared_ring.resolve("K") == "fresh-key"

    asyncio.run(run())

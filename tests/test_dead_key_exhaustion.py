"""Regression tests for the two confirmed dead-key bugs.

Bug A: one dead-key model in the pool used to hang ``ask()`` forever under the
default ``wait=None`` — nothing was ever going to notify the waiter.
Bug B: the debounced rebuild's registry resync could resurrect a just-dropped
dead-key model, since it ran before the journal-derived drop was reapplied.
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


def test_dead_key_drop_survives_rebuild(tmp_path):
    async def run():
        async with AsyncBroker(
            registry=_registry(tmp_path),
            secrets=DictSecrets({"K": "dead-key"}),
            store=FileStore(tmp_path / "store"),
            sync=None,
        ) as broker:
            call_provider = AsyncMock(side_effect=_http_status_error(401))
            with patch(_PATCH, new=call_provider):
                with pytest.raises(NoLLMAvailableError):
                    await asyncio.wait_for(broker.ask("hi"), timeout=5)
            assert call_provider.call_count == 1
            assert "p1" not in await broker.snapshot()

            await broker._learner.maybe_rebuild(force=True)
            await broker._learner.maybe_rebuild(force=True)

            assert "p1" not in await broker.snapshot()
            with patch(_PATCH, new=call_provider):
                with pytest.raises(NoLLMAvailableError):
                    await asyncio.wait_for(broker.ask("hi"), timeout=5)
            assert call_provider.call_count == 1  # never re-attempted

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
            assert "p1" not in await broker.snapshot()

            secrets._mapping["K"] = "fresh-key"  # noqa: SLF001 - test double, direct mutation
            await broker._learner.maybe_rebuild(force=True)

            snap = await broker.snapshot()
            assert "p1" in snap
            assert snap["p1"].has_key is True

    asyncio.run(run())

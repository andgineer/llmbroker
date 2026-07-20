"""Broker-level direct() access and the pooled flag — mocked httpx, no network."""

import asyncio
from unittest.mock import patch

import httpx
import pytest

from llmbroker.broker.broker import AsyncBroker
from llmbroker.exceptions import MissingKeyError, UnknownModelError
from llmbroker.models import LLMConfig
from llmbroker.standalone.registry import Registry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import FileStore
from llmbroker.sync import Broker

_BODY = """
[[llms]]
name="pooled-a"
base_url="https://pool/v1"
model="m"
api_key_ref="K"

[[llms]]
name="frontier"
base_url="https://paid/v1"
model="big"
api_key_ref="K"
pool=false
"""

_SSE = (
    b'data: {"choices": [{"delta": {"content": "Hel"}}]}\n\n'
    b'data: {"choices": [{"delta": {"content": "lo"}}]}\n\n'
    b"data: [DONE]\n\n"
)


def _registry(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text(_BODY)
    return Registry(f)


def _broker(tmp_path, secrets=None) -> AsyncBroker:
    return AsyncBroker(
        registry=_registry(tmp_path),
        secrets=secrets or DictSecrets({"K": "test"}),
        store=FileStore(tmp_path / "store"),
    )


# --------------------------------------------------------------------------- #
# pooled flag
# --------------------------------------------------------------------------- #


def test_pooled_default_and_metadata_roundtrip():
    assert LLMConfig(name="a", base_url="u", model="m", api_key_ref="K").pooled is True
    direct = LLMConfig(name="a", base_url="u", model="m", api_key_ref="K", pooled=False)
    assert direct.to_metadata() == {"pool": False}
    back = LLMConfig.from_metadata(
        name="a", base_url="u", model="m", api_key_ref="K", metadata={"pool": False}
    )
    assert back.pooled is False


def test_file_registry_parses_pool_false(tmp_path):
    configs = {c.name: c for c in asyncio.run(_registry(tmp_path).load())}
    assert configs["pooled-a"].pooled is True
    assert configs["frontier"].pooled is False


def test_pool_false_excluded_from_pool(tmp_path):
    async def run():
        async with _broker(tmp_path) as broker:
            return await broker.count()

    assert asyncio.run(run()) == 1  # only pooled-a joins the routed pool


# --------------------------------------------------------------------------- #
# broker.direct()
# --------------------------------------------------------------------------- #


def test_direct_streams_paid_model(tmp_path):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, content=_SSE, headers={"content-type": "text/event-stream"})

    mock = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=1.0)

    async def run():
        with patch("llmbroker.broker.broker.make_client", return_value=mock):
            async with _broker(tmp_path) as broker:
                client = await broker.direct("frontier")
                return [d async for d in client.stream("hi")]

    deltas = asyncio.run(run())
    assert deltas == ["Hel", "lo"]
    assert seen["url"] == "https://paid/v1/chat/completions"


def test_direct_works_on_pool_model(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        body = {"choices": [{"message": {"role": "assistant", "content": "direct-pool"}}]}
        return httpx.Response(200, json=body)

    mock = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=1.0)

    async def run():
        with patch("llmbroker.broker.broker.make_client", return_value=mock):
            async with _broker(tmp_path) as broker:
                client = await broker.direct("pooled-a")
                return await client.ask("hi")

    assert asyncio.run(run()).text == "direct-pool"


def test_direct_unknown_model_raises(tmp_path):
    async def run():
        async with _broker(tmp_path) as broker:
            with pytest.raises(UnknownModelError):
                await broker.direct("nope")

    asyncio.run(run())


def test_direct_missing_key_raises(tmp_path):
    async def run():
        async with _broker(tmp_path, secrets=DictSecrets({})) as broker:
            with pytest.raises(MissingKeyError):
                await broker.direct("frontier")

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# sync Broker.direct()
# --------------------------------------------------------------------------- #


def test_sync_broker_direct_ask(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        body = {"choices": [{"message": {"role": "assistant", "content": "sync-direct"}}]}
        return httpx.Response(200, json=body)

    mock = httpx.Client(transport=httpx.MockTransport(handler), timeout=1.0)

    with patch("llmbroker.direct.httpx.Client", return_value=mock):
        with Broker(
            registry=_registry(tmp_path),
            secrets=DictSecrets({"K": "test"}),
            store=FileStore(tmp_path / "store"),
        ) as broker:
            result = broker.direct("frontier").ask("hi")

    assert result.text == "sync-direct"

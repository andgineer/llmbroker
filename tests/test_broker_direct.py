"""Broker-level direct() access and the pool boundary — mocked httpx, no network."""

import asyncio
from unittest.mock import patch

import httpx
import pytest

from llmbroker.broker.broker import AsyncBroker
from llmbroker.broker.catalog import find_declared
from llmbroker.exceptions import MissingKeyError, PoolModelError, UnknownModelError
from llmbroker.models import LLMConfig
from llmbroker.standalone.registry import Registry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import FileStore
from llmbroker.sync import Broker

_BODY = """
[[llms]]
name="managed-a"
base_url="https://pool/v1"
model="m"
api_key_ref="K"
"""

_DECLARED = [
    LLMConfig(
        name="frontier",
        alias="opus",
        base_url="https://paid/v1",
        model="big",
        api_key_ref="K",
    ),
    LLMConfig(name="extra", base_url="https://extra/v1", model="m", api_key_ref="K"),
]

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
        sync=None,
        direct=_DECLARED,
    )


# --------------------------------------------------------------------------- #
# the pool boundary
# --------------------------------------------------------------------------- #


def test_the_registry_holds_pool_members_only(tmp_path):
    configs = asyncio.run(_registry(tmp_path).load())
    assert [c.name for c in configs] == ["managed-a"]


def test_the_pool_is_the_curated_lineup_and_takes_no_declared_model(tmp_path):
    """Rule 1: nothing a host declares in code ever joins the routed pool — the pool
    is exactly what the registry holds."""

    async def run():
        async with _broker(tmp_path) as broker:
            return set(await broker.snapshot())

    assert asyncio.run(run()) == {"managed-a"}


# --------------------------------------------------------------------------- #
# broker.direct()
# --------------------------------------------------------------------------- #


def test_direct_streams_paid_model_by_alias(tmp_path):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, content=_SSE, headers={"content-type": "text/event-stream"})

    mock = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=1.0)

    async def run():
        with patch("llmbroker.broker.broker.make_client", return_value=mock):
            async with _broker(tmp_path) as broker:
                client = await broker.direct("opus")
                return [d async for d in client.stream("hi")]

    deltas = asyncio.run(run())
    assert deltas == ["Hel", "lo"]
    assert seen["url"] == "https://paid/v1/chat/completions"


def test_direct_by_name_pins_the_version(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        body = {"choices": [{"message": {"role": "assistant", "content": "pinned"}}]}
        return httpx.Response(200, json=body)

    mock = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=1.0)

    async def run():
        with patch("llmbroker.broker.broker.make_client", return_value=mock):
            async with _broker(tmp_path) as broker:
                client = await broker.direct(name="extra")
                return await client.ask("hi")

    assert asyncio.run(run()).text == "pinned"


def test_direct_refuses_pool_model(tmp_path):
    async def run():
        async with _broker(tmp_path) as broker:
            with pytest.raises(PoolModelError, match="anonymous"):
                await broker.direct(name="managed-a")

    asyncio.run(run())


def test_direct_requires_exactly_one_keyspace(tmp_path):
    async def run():
        async with _broker(tmp_path) as broker:
            with pytest.raises(ValueError, match="exactly one"):
                await broker.direct()
            with pytest.raises(ValueError, match="exactly one"):
                await broker.direct("opus", name="frontier")

    asyncio.run(run())


def test_direct_unknown_model_raises(tmp_path):
    async def run():
        async with _broker(tmp_path) as broker:
            with pytest.raises(UnknownModelError):
                await broker.direct("nope")

    asyncio.run(run())


def test_direct_cross_keyspace_miss_names_the_other_keyspace(tmp_path):
    async def run():
        async with _broker(tmp_path) as broker:
            with pytest.raises(UnknownModelError, match=r"call direct\(name='frontier'\)"):
                await broker.direct("frontier")
            with pytest.raises(UnknownModelError, match=r"call direct\('opus'\)"):
                await broker.direct(name="opus")

    asyncio.run(run())


def test_direct_positional_pool_name_says_pool_straight_away(tmp_path):
    """The pre-alias call shape. Routing it through the name keyspace first would
    only spend an error to arrive at the same PoolModelError."""

    async def run():
        async with _broker(tmp_path) as broker:
            with pytest.raises(PoolModelError, match="anonymous"):
                await broker.direct("managed-a")

    asyncio.run(run())


def test_direct_by_name_searches_the_declared_models_only():
    """`check_overlay` refuses this collision at provision, so it is unreachable
    through a broker — but the lookup itself must still mean the declared model."""
    managed = LLMConfig(name="dup", base_url="https://pool/v1", model="m", api_key_ref="K")
    mine = LLMConfig(name="dup", base_url="https://paid/v1", model="big", api_key_ref="K")
    assert find_declared([managed], [mine], None, "dup") is mine
    with pytest.raises(PoolModelError):
        find_declared([managed], [], None, "dup")


def test_direct_missing_key_raises(tmp_path):
    async def run():
        async with _broker(tmp_path, secrets=DictSecrets({})) as broker:
            with pytest.raises(MissingKeyError):
                await broker.direct("opus")

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
            sync=None,
            direct=_DECLARED,
        ) as broker:
            result = broker.direct("opus").ask("hi")
            with pytest.raises(PoolModelError):
                broker.direct(name="managed-a")

    assert result.text == "sync-direct"

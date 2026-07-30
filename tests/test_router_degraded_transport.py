"""Degraded-transport failover: every way a call can fail below the HTTP status
layer must cool the model, fail over, and return its slot to the pool.
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from llmbroker.broker.broker import AsyncBroker
from llmbroker.broker.pool import LLMPool
from llmbroker.broker.router import Router
from llmbroker.chat import call_provider
from llmbroker.exceptions import InvalidProviderResponseError, NoLLMAvailableError
from llmbroker.models import CallStatus, LifecyclePhase, LLMConfig
from llmbroker.sqlite import Store as SqliteStore
from llmbroker.standalone.registry import Registry as FileRegistry
from llmbroker.standalone.secrets import DictSecrets

_PATCH = "llmbroker.broker.router.call_provider"


class _RecordingStore:
    def __init__(self) -> None:
        self.calls: list = []

    async def record(self, call):
        self.calls.append(call)

    async def record_quality(self, llm_name, operation, score, *, call_id=None):
        pass


def _cfg(name="p1", model="m"):
    return LLMConfig(name=name, base_url="https://x/v1", model=model, api_key_ref="K")


async def _pool(*cfgs) -> LLMPool:
    pool = LLMPool()
    for order, cfg in enumerate(cfgs):
        await pool.add(cfg, "secret", order)
    return pool


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=1.0)


async def _call_via_transport(handler, name="p1"):
    """Run ``call_provider`` over a MockTransport — the real json/shape path."""
    async with _mock_client(handler) as client:
        return await call_provider(_cfg(name), "secret", [], None, client=client)


# ---------------------------------------------------------------------------
# httpx transport errors that are not OSError subclasses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadError("peer went away"),
        httpx.WriteError("broken pipe"),
        httpx.RemoteProtocolError("bad chunk"),
        httpx.ProxyError("proxy refused"),
        httpx.ConnectError("refused"),
    ],
    ids=["read", "write", "protocol", "proxy", "connect"],
)
def test_transport_error_cools_and_fails_over(exc):
    async def run():
        pool = await _pool(_cfg("a"), _cfg("b"))
        store = _RecordingStore()
        router = Router(pool, store, scope=None)
        with patch(_PATCH, new=AsyncMock(side_effect=[exc, ("ok", None, None)])):
            result = await router.chat([{"role": "user", "content": "hi"}])

        assert result.text == "ok"
        assert pool.state("a").phase is LifecyclePhase.COOLING
        row_a = next(c for c in store.calls if c.llm_name == "a")
        assert row_a.error_detail == type(exc).__name__
        assert pool._slots["a"].in_flight == 0
        assert pool._slots["b"].in_flight == 0

    asyncio.run(run())


def test_single_model_transport_failure_raises_no_llm_available_not_httpx():
    async def run():
        pool = await _pool(_cfg())
        router = Router(pool, _RecordingStore(), scope=None)
        with patch(_PATCH, new=AsyncMock(side_effect=httpx.ReadError("peer went away"))):
            with pytest.raises(NoLLMAvailableError) as exc_info:
                await router.chat([{"role": "user", "content": "hi"}], wait=0)
        assert exc_info.value.reason == "timeout"
        assert pool._slots["p1"].in_flight == 0

    asyncio.run(run())


# ---------------------------------------------------------------------------
# HTTP 200 with a body that is not a chat completion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "snippet"),
    [
        (httpx.Response(200, text="<html>gateway</html>"), "gateway"),
        (httpx.Response(200, json={"error": {"message": "quota exceeded"}}), "quota exceeded"),
        (httpx.Response(200, json={"choices": []}), "choices"),
        (httpx.Response(200, json={"choices": [{"text": "no message key"}]}), "no message key"),
        (httpx.Response(200, json=["not", "an", "object"]), "not"),
    ],
    ids=["invalid_json", "error_body", "empty_choices", "no_message", "not_an_object"],
)
def test_malformed_200_raises_invalid_provider_response(response, snippet):
    async def run():
        with pytest.raises(InvalidProviderResponseError) as exc_info:
            await _call_via_transport(lambda request: response)
        assert exc_info.value.model == "p1"
        assert snippet in exc_info.value.detail

    asyncio.run(run())


def test_unusable_token_counts_do_not_escape_as_a_raw_error():
    """A 200 whose `usage` holds `1e400` (valid JSON, decodes to `inf`) used to hand
    the caller a raw OverflowError past every failover path. The answer is good, so
    the right outcome is the answer — with the unusable number dropped."""

    async def run():
        pool = await _pool(_cfg())
        store = _RecordingStore()
        router = Router(pool, store, scope=None)
        body = (
            b'{"choices":[{"message":{"content":"hi"}}],"usage":{"prompt_tokens":3,"cost":1e400}}'
        )
        router._http = _mock_client(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=body,
            ),
        )
        try:
            result = await router.chat([{"role": "user", "content": "hi"}])
        finally:
            await router.aclose()

        assert result.text == "hi"
        assert result.usage is not None
        assert result.usage.prompt_tokens == 3
        assert result.usage.extra is None
        assert store.calls[0].status is CallStatus.OK
        assert pool.state("p1").phase is LifecyclePhase.AVAILABLE

    asyncio.run(run())


def test_oversized_token_count_still_reaches_a_real_journal(tmp_path):
    """The `1e400` guard only covered floats: a finite 30-digit count sailed through
    into the insert, where a 64-bit column rejected it — and `_log_call` swallows
    that, so a served call vanished from the journal without the caller noticing."""

    async def run():
        config = tmp_path / "llms.toml"
        config.write_text(
            '[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n'
        )
        body = {
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": int("9" * 30), "completion_tokens": 4},
        }
        broker = AsyncBroker(
            registry=FileRegistry(config),
            secrets=DictSecrets({"K": "k"}),
            store=SqliteStore(str(tmp_path / "s.db")),
        )
        async with broker:
            broker._router._http = _mock_client(lambda request: httpx.Response(200, json=body))
            result = await broker.ask("hi")
            assert result.text == "hi"
            assert result.usage is not None
            assert result.usage.prompt_tokens is None
            assert result.usage.completion_tokens == 4
            rows = await broker.calls(limit=10)
        assert [row.status for row in rows] == [CallStatus.OK]

    asyncio.run(run())


def test_malformed_200_cools_and_fails_over_over_real_transport():
    """No patching of ``call_provider``: the garbage body travels the real json path."""

    async def run():
        pool = await _pool(_cfg("a", model="a-model"), _cfg("b", model="b-model"))
        store = _RecordingStore()
        router = Router(pool, store, scope=None)

        def handler(request: httpx.Request) -> httpx.Response:
            if json.loads(request.content)["model"] == "a-model":
                return httpx.Response(200, json={"error": "no choices here"})
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        router._http = _mock_client(handler)
        try:
            result = await router.chat([{"role": "user", "content": "hi"}])
        finally:
            await router.aclose()

        assert result.text == "ok"
        assert result.llm_name == "b"
        assert pool.state("a").phase is LifecyclePhase.COOLING
        row_a = next(c for c in store.calls if c.llm_name == "a")
        assert "no choices here" in row_a.error_detail
        assert row_a.cooldown_until is not None
        assert pool._slots["a"].in_flight == 0

    asyncio.run(run())


def test_invalid_json_over_real_transport_leaves_no_slot_leaked():
    async def run():
        pool = await _pool(_cfg())
        router = Router(pool, _RecordingStore(), scope=None)
        router._http = _mock_client(lambda request: httpx.Response(200, text="not json"))
        try:
            with pytest.raises(NoLLMAvailableError):
                await router.chat([{"role": "user", "content": "hi"}], wait=0)
        finally:
            await router.aclose()
        assert pool._slots["p1"].in_flight == 0

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Last-resort guard: an unexpected exception surfaces but never leaks the slot
# ---------------------------------------------------------------------------


def test_cancelled_call_releases_the_slot():
    """A host that cancels (a disconnected client, an `asyncio.timeout`) must not
    cost the model a unit of its `parallel` capacity forever."""

    async def run():
        cfg = LLMConfig(name="p1", base_url="https://x/v1", model="m", api_key_ref="K", parallel=1)
        pool = LLMPool()
        await pool.add(cfg, "secret", 0)
        router = Router(pool, _RecordingStore(), scope=None)

        async def hang(*args, **kwargs):
            await asyncio.sleep(60)

        with patch(_PATCH, new=hang):
            task = asyncio.create_task(router.chat([{"role": "user", "content": "hi"}]))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert pool._slots["p1"].in_flight == 0
        # the cap is intact: the next caller still gets the only slot
        assert await asyncio.wait_for(pool.acquire(None), 0.5) is cfg

    asyncio.run(run())


def test_host_timeout_around_the_call_releases_the_slot():
    """The host's own `asyncio.timeout` nests around the router's: the outer scope
    fires, the inner one must not swallow it, and the slot still comes back."""

    async def run():
        cfg = LLMConfig(name="p1", base_url="https://x/v1", model="m", api_key_ref="K", parallel=1)
        pool = LLMPool()
        await pool.add(cfg, "secret", 0)
        router = Router(pool, _RecordingStore(), scope=None)

        async def hang(*args, **kwargs):
            await asyncio.sleep(60)

        with patch(_PATCH, new=hang):
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(0.05):
                    await router.chat([{"role": "user", "content": "hi"}])

        assert pool._slots["p1"].in_flight == 0
        assert await asyncio.wait_for(pool.acquire(None), 0.5) is cfg

    asyncio.run(run())


def test_unexpected_exception_propagates_and_releases_the_slot():
    async def run():
        pool = await _pool(_cfg())
        store = _RecordingStore()
        router = Router(pool, store, scope=None)
        with patch(_PATCH, new=AsyncMock(side_effect=RuntimeError("planted bug"))):
            with pytest.raises(RuntimeError, match="planted bug"):
                await router.chat([{"role": "user", "content": "hi"}])
        assert pool._slots["p1"].in_flight == 0
        assert store.calls[0].error_detail == "RuntimeError"
        assert store.calls[0].cooldown_until is None

    asyncio.run(run())

"""Tests for the tool loop and the dispatch it runs tools through."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from llmbroker.broker.broker import AsyncBroker
from llmbroker.direct import AsyncDirectClient, DirectClient, DirectResult
from llmbroker.exceptions import ToolLoopLimitError
from llmbroker.models import LLMConfig, Usage
from llmbroker.standalone.registry import Registry
from llmbroker.standalone.secrets import DictSecrets
from llmbroker.standalone.store import FileStore, InMemoryStore
from llmbroker.sync import Broker
from llmbroker.tool_loop import arun_tool_loop, execute_tool_calls, run_tool_loop


def test_execute_tool_calls_success():
    calls = [{"id": "1", "function": {"name": "echo", "arguments": '{"msg": "hi"}'}}]
    results = execute_tool_calls(calls, {"echo": lambda msg: f"echoed:{msg}"})
    assert len(results) == 1
    assert results[0]["role"] == "tool"
    assert "echoed:hi" in results[0]["content"]
    assert results[0]["tool_call_id"] == "1"


def test_execute_tool_calls_unknown_tool():
    calls = [{"id": "1", "function": {"name": "ghost", "arguments": "{}"}}]
    results = execute_tool_calls(calls, {})
    assert "Unknown tool ghost" in results[0]["content"]


def test_execute_tool_calls_tool_raises():
    def _boom():
        raise ValueError("exploded")

    calls = [{"id": "1", "function": {"name": "boom", "arguments": "{}"}}]
    results = execute_tool_calls(calls, {"boom": _boom})
    assert "exploded" in results[0]["content"]


def test_execute_tool_calls_bad_json_args():
    calls = [{"id": "1", "function": {"name": "f", "arguments": "not-json"}}]
    results = execute_tool_calls(calls, {"f": lambda: "ok"})
    assert results[0]["content"] == "ok"


def test_arun_tool_loop_no_tool_calls():
    result = MagicMock()
    result.tool_calls = None
    result.text = "done"
    llms = MagicMock()
    llms.chat = AsyncMock(return_value=result)
    reply = asyncio.run(arun_tool_loop(llms, [{"role": "user", "content": "hi"}]))
    assert reply.text == "done"
    assert llms.chat.call_count == 1


def test_arun_tool_loop_with_tool_then_reply():
    tool_result = MagicMock()
    tool_result.tool_calls = [
        {"id": "1", "function": {"name": "add", "arguments": '{"a": 1, "b": 2}'}}
    ]
    tool_result.text = None

    final_result = MagicMock()
    final_result.tool_calls = None
    final_result.text = "3"

    llms = MagicMock()
    llms.chat = AsyncMock(side_effect=[tool_result, final_result])

    reply = asyncio.run(arun_tool_loop(llms, [], dispatch={"add": lambda a, b: a + b}))
    assert reply.text == "3"
    assert llms.chat.call_count == 2


def test_tool_loop_returns_the_final_result_not_text():
    """The loop hands back the routed call the final reply came from — text alone
    would drop `usage` and the identity the router held."""
    final_result = MagicMock()
    final_result.tool_calls = None
    final_result.text = "3"
    final_result.call_id = "c-2"
    final_result.usage = Usage(prompt_tokens=5, completion_tokens=1, total_tokens=6)

    llms = MagicMock()
    llms.chat = MagicMock(return_value=final_result)

    reply = run_tool_loop(llms, [])
    assert reply is final_result
    assert (reply.text, reply.call_id, reply.usage.total_tokens) == ("3", "c-2", 6)


def test_tool_loop_result_names_the_model_of_the_final_round():
    """Each round is a routed call of its own: the result names the model that
    produced the final reply, not the one that asked for the tool."""
    tool_result = MagicMock()
    tool_result.tool_calls = [{"id": "1", "function": {"name": "add", "arguments": "{}"}}]
    tool_result.text = None
    tool_result.llm_name = "first"

    final_result = MagicMock()
    final_result.tool_calls = None
    final_result.text = "3"
    final_result.llm_name = "second"

    llms = MagicMock()
    llms.chat = AsyncMock(side_effect=[tool_result, final_result])

    reply = asyncio.run(arun_tool_loop(llms, [], dispatch={"add": lambda: 3}))
    assert reply.llm_name == "second"


def test_tool_loop_over_a_real_broker_hands_back_the_routed_call(tmp_path):
    """Against a real broker rather than a mock: what comes back is the routed call
    itself, so the shape the docs teach — text plus the model that produced it — holds."""
    f = tmp_path / "llms.toml"
    f.write_text('[[llms]]\nname="p1"\nbase_url="https://x/v1"\nmodel="m"\napi_key_ref="K"\n')
    rounds = [
        ("", [{"id": "1", "function": {"name": "add", "arguments": '{"a": 1, "b": 2}'}}], None),
        ("3", None, Usage(prompt_tokens=9, completion_tokens=2, total_tokens=11)),
    ]

    with (
        Broker(
            registry=Registry(f),
            secrets=DictSecrets({"K": "test"}),
            store=InMemoryStore(),
            sync=None,
        ) as broker,
        patch(
            "llmbroker.broker.router.call_provider",
            new=AsyncMock(side_effect=rounds),
        ) as provider,
    ):
        reply = run_tool_loop(
            broker,
            [{"role": "user", "content": "1 + 2?"}],
            dispatch={"add": lambda a, b: a + b},
        )

    assert provider.await_count == 2
    assert (reply.text, reply.llm_name) == ("3", "p1")
    assert reply.usage.total_tokens == 11
    assert reply.call_id


def _always_wants_tools() -> MagicMock:
    result = MagicMock()
    result.tool_calls = [{"id": "1", "function": {"name": "f", "arguments": "{}"}}]
    result.text = None
    return result


def test_arun_tool_loop_max_steps_raises_naming_the_limit():
    llms = MagicMock()
    llms.chat = AsyncMock(return_value=_always_wants_tools())
    with pytest.raises(ToolLoopLimitError, match="max_steps=2"):
        asyncio.run(arun_tool_loop(llms, [], max_steps=2))
    assert llms.chat.call_count == 2


def test_run_tool_loop_max_steps_raises_naming_the_limit():
    llms = MagicMock()
    llms.chat = MagicMock(return_value=_always_wants_tools())
    with pytest.raises(ToolLoopLimitError, match="max_steps=3"):
        run_tool_loop(llms, [], max_steps=3)
    assert llms.chat.call_count == 3


# ── A direct client drives the same loop ─────────────────────────────────────

_ADD_TOOL = [{"type": "function", "function": {"name": "add", "parameters": {}}}]
_ADD_CALL = {
    "id": "1",
    "type": "function",
    "function": {"name": "add", "arguments": '{"a": 1, "b": 2}'},
}


class _Provider:
    """An OpenAI-compatible endpoint that asks for ``add`` once, then answers."""

    def __init__(self, *, forever: bool = False) -> None:
        self.bodies: list[dict] = []
        self._forever = forever

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        if self._forever or len(self.bodies) == 1:
            message = {"role": "assistant", "content": None, "tool_calls": [_ADD_CALL]}
        else:
            message = {"role": "assistant", "content": "3"}
        return httpx.Response(
            200, json={"choices": [{"message": message}], "usage": {"total_tokens": 5}}
        )


def _assert_one_tool_round(provider: _Provider, reply) -> None:
    assert isinstance(reply, DirectResult)
    assert (reply.text, reply.tool_calls, reply.usage.total_tokens) == ("3", None, 5)
    assert len(provider.bodies) == 2
    assert all(b["tools"] == _ADD_TOOL and b["tool_choice"] == "auto" for b in provider.bodies)
    assert provider.bodies[1]["messages"][1:] == [
        {"role": "assistant", "content": None, "tool_calls": [_ADD_CALL]},
        {"role": "tool", "tool_call_id": "1", "content": "3"},
    ]


def test_arun_tool_loop_drives_an_async_direct_client_end_to_end():
    provider = _Provider()

    async def run():
        async with AsyncDirectClient(
            base_url="https://paid/v1",
            model="big",
            api_key="k",
            client=httpx.AsyncClient(transport=httpx.MockTransport(provider)),
        ) as client:
            return await arun_tool_loop(
                client,
                [{"role": "user", "content": "1 + 2?"}],
                tools=_ADD_TOOL,
                dispatch={"add": lambda a, b: a + b},
                params={"temperature": 0},
            )

    reply = asyncio.run(run())
    _assert_one_tool_round(provider, reply)
    assert all(b["temperature"] == 0 for b in provider.bodies)


def test_run_tool_loop_drives_a_sync_direct_client_end_to_end():
    provider = _Provider()
    with DirectClient(
        base_url="https://paid/v1",
        model="big",
        api_key="k",
        client=httpx.Client(transport=httpx.MockTransport(provider)),
    ) as client:
        reply = run_tool_loop(
            client,
            [{"role": "user", "content": "1 + 2?"}],
            tools=_ADD_TOOL,
            dispatch={"add": lambda a, b: a + b},
        )
    _assert_one_tool_round(provider, reply)


def test_a_direct_client_still_hits_the_step_limit():
    provider = _Provider(forever=True)
    with (
        DirectClient(
            base_url="https://paid/v1",
            model="big",
            api_key="k",
            client=httpx.Client(transport=httpx.MockTransport(provider)),
        ) as client,
        pytest.raises(ToolLoopLimitError, match="max_steps=2"),
    ):
        run_tool_loop(
            client, [], tools=_ADD_TOOL, dispatch={"add": lambda a, b: a + b}, max_steps=2
        )
    assert len(provider.bodies) == 2


async def test_arun_tool_loop_over_an_async_direct_client_hits_the_step_limit():
    provider = _Provider(forever=True)
    async with AsyncDirectClient(
        base_url="https://paid/v1",
        model="big",
        api_key="k",
        client=httpx.AsyncClient(transport=httpx.MockTransport(provider)),
    ) as client:
        with pytest.raises(ToolLoopLimitError, match="max_steps=3"):
            await arun_tool_loop(client, [], tools=_ADD_TOOL, max_steps=3)
    assert len(provider.bodies) == 3


_POOL = '[[llms]]\nname="p1"\nbase_url="https://pool/v1"\nmodel="m"\napi_key_ref="K"\n'
_PAID = LLMConfig(
    name="frontier", alias="opus", base_url="https://paid/v1", model="big", api_key_ref="K"
)


def test_a_tool_loop_over_a_brokers_sync_direct_client_writes_no_journal_row(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text(_POOL)
    provider = _Provider()
    with (
        patch(
            "llmbroker.direct.httpx.Client",
            return_value=httpx.Client(transport=httpx.MockTransport(provider)),
        ),
        patch(
            "llmbroker.broker.router.call_provider", new=AsyncMock(return_value=("ok", None, None))
        ),
        Broker(
            registry=Registry(f),
            secrets=DictSecrets({"K": "k"}),
            store=FileStore(tmp_path / "store"),
            sync=None,
            direct=[_PAID],
        ) as broker,
    ):
        reply = run_tool_loop(
            broker.direct("opus"),
            [{"role": "user", "content": "1 + 2?"}],
            tools=_ADD_TOOL,
            dispatch={"add": lambda a, b: a + b},
        )
        after_loop = broker.calls(limit=10)
        broker.ask("and a routed call journals")
        after_routed = broker.calls(limit=10)

    _assert_one_tool_round(provider, reply)
    assert after_loop == []
    assert [c.llm_name for c in after_routed] == ["p1"]


async def test_a_tool_loop_over_a_brokers_async_direct_client_writes_no_journal_row(tmp_path):
    f = tmp_path / "llms.toml"
    f.write_text(_POOL)
    provider = _Provider()
    mock = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    with patch("llmbroker.chat.make_client", return_value=mock):
        async with AsyncBroker(
            registry=Registry(f),
            secrets=DictSecrets({"K": "k"}),
            store=FileStore(tmp_path / "store"),
            sync=None,
            direct=[_PAID],
        ) as broker:
            reply = await arun_tool_loop(
                await broker.direct("opus"),
                [{"role": "user", "content": "1 + 2?"}],
                tools=_ADD_TOOL,
                dispatch={"add": lambda a, b: a + b},
            )
            rows = await broker.calls(limit=10)

    _assert_one_tool_round(provider, reply)
    assert rows == []


# ── A catalog alias takes tools with no host parameters ──────────────────────


def test_run_tool_loop_over_a_catalog_alias_sends_its_tool_params_every_round(
    tmp_path,
    bundled_presets,
):
    """The shipped `gpt-fast` line refuses function tools unless reasoning is off; the
    catalog says so, so the host passes nothing and every round is accepted."""
    provider = _Provider()
    with (
        patch(
            "llmbroker.direct.httpx.Client",
            return_value=httpx.Client(transport=httpx.MockTransport(provider)),
        ),
        Broker(
            secrets=DictSecrets({"OPENAI_API_KEY": "k"}),
            store=InMemoryStore(),
            home=tmp_path / "home",
            sync=None,
            direct=["gpt-fast"],
        ) as broker,
    ):
        reply = run_tool_loop(
            broker.direct("gpt-fast"),
            [{"role": "user", "content": "1 + 2?"}],
            tools=_ADD_TOOL,
            dispatch={"add": lambda a, b: a + b},
        )
        answered = broker.direct("gpt-fast").ask("no tools, the model's own defaults")

    assert answered.text == "3"
    assert len(provider.bodies) == 3
    assert [b.get("reasoning_effort") for b in provider.bodies] == ["none", "none", None]
    assert reply.text == "3"


async def test_arun_tool_loop_over_a_catalog_alias_lets_the_host_override_its_tool_params(
    tmp_path,
    bundled_presets,
):
    provider = _Provider()
    mock = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    with patch("llmbroker.chat.make_client", return_value=mock):
        async with AsyncBroker(
            secrets=DictSecrets({"OPENAI_API_KEY": "k"}),
            store=InMemoryStore(),
            home=tmp_path / "home",
            sync=None,
            direct=["gpt-fast"],
        ) as broker:
            await arun_tool_loop(
                await broker.direct("gpt-fast"),
                [{"role": "user", "content": "1 + 2?"}],
                tools=_ADD_TOOL,
                dispatch={"add": lambda a, b: a + b},
            )
            await arun_tool_loop(
                await broker.direct("gpt-fast"),
                [{"role": "user", "content": "1 + 2?"}],
                tools=_ADD_TOOL,
                dispatch={"add": lambda a, b: a + b},
                params={"reasoning_effort": "low"},
            )

    efforts = [b["reasoning_effort"] for b in provider.bodies]
    assert efforts[:2] == ["none", "none"]
    assert set(efforts[2:]) == {"low"}

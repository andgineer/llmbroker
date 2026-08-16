"""Tests for the tool loop and the dispatch it runs tools through."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from llmbroker.exceptions import ToolLoopLimitError
from llmbroker.models import Usage
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

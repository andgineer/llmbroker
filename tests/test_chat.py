"""Tests for the chat.py primitives and tool loop."""

import asyncio
from unittest.mock import AsyncMock, MagicMock


from llmbroker.chat import (
    arun_tool_loop,
    build_chat_request,
    execute_tool_calls,
    is_rate_limit,
    message_from_response,
    parse_tool_calls,
    parse_usage,
    retry_after_seconds,
)
from llmbroker.models import LLMConfig

_CONFIG = LLMConfig(
    name="p1",
    base_url="https://api.example.com/v1",
    model="gpt-4o",
    api_key_ref="K",
)


def test_is_rate_limit_429():
    assert is_rate_limit(429) is True


def test_is_rate_limit_503():
    assert is_rate_limit(503) is True


def test_is_rate_limit_500():
    assert is_rate_limit(500) is False


def test_is_rate_limit_200():
    assert is_rate_limit(200) is False


def test_retry_after_seconds_from_header():
    assert retry_after_seconds({"Retry-After": "30"}, 60) == 30


def test_retry_after_seconds_default_when_missing():
    assert retry_after_seconds({}, 60) == 60


def test_retry_after_seconds_default_on_bad_value():
    assert retry_after_seconds({"Retry-After": "soon"}, 45) == 45


def test_build_chat_request_basic():
    url, headers, body = build_chat_request(_CONFIG, "the-key", [{"role": "user", "content": "hi"}])
    assert url == "https://api.example.com/v1/chat/completions"
    assert headers["Authorization"] == "Bearer the-key"
    assert body["model"] == "gpt-4o"
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert "tools" not in body


def test_build_chat_request_with_tools():
    tools = [{"type": "function", "function": {"name": "f"}}]
    _, _, body = build_chat_request(_CONFIG, "k", [], tools=tools)
    assert body["tools"] == tools
    assert body["tool_choice"] == "auto"


def test_message_from_response():
    data = {"choices": [{"message": {"role": "assistant", "content": "hello"}}]}
    assert message_from_response(data) == {"role": "assistant", "content": "hello"}


def test_parse_usage_full():
    data = {"usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}}
    u = parse_usage(data)
    assert u is not None
    assert u.prompt_tokens == 10
    assert u.completion_tokens == 20
    assert u.total_tokens == 30
    assert u.extra is None


def test_parse_usage_missing():
    assert parse_usage({}) is None


def test_parse_usage_non_dict():
    assert parse_usage({"usage": "text"}) is None


def test_parse_usage_extra_fields():
    data = {
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 2,
            "total_tokens": 3,
            "cache_read_input_tokens": 5,
        }
    }
    u = parse_usage(data)
    assert u is not None
    assert u.extra == {"cache_read_input_tokens": 5}


def test_parse_tool_calls_present():
    calls = [{"id": "c1", "function": {"name": "f", "arguments": "{}"}}]
    assert parse_tool_calls({"tool_calls": calls}) == calls


def test_parse_tool_calls_absent():
    assert parse_tool_calls({}) is None


def test_parse_tool_calls_empty_list():
    assert parse_tool_calls({"tool_calls": []}) is None


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
    text = asyncio.run(arun_tool_loop(llms, [{"role": "user", "content": "hi"}]))
    assert text == "done"
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

    text = asyncio.run(arun_tool_loop(llms, [], dispatch={"add": lambda a, b: a + b}))
    assert text == "3"
    assert llms.chat.call_count == 2


def test_arun_tool_loop_max_steps_returns_empty():
    result = MagicMock()
    result.tool_calls = [{"id": "1", "function": {"name": "f", "arguments": "{}"}}]
    result.text = None
    llms = MagicMock()
    llms.chat = AsyncMock(return_value=result)
    text = asyncio.run(arun_tool_loop(llms, [], max_steps=2))
    assert text == ""
    assert llms.chat.call_count == 2

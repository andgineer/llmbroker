"""Tests for the chat.py primitives and tool loop."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from llmbroker.chat import (
    _parse_completion,
    arun_tool_loop,
    build_chat_request,
    execute_tool_calls,
    is_rate_limit,
    message_from_response,
    parse_tool_calls,
    parse_usage,
    retry_after_seconds,
    run_tool_loop,
)
from llmbroker.exceptions import InvalidProviderResponseError, ToolLoopLimitError
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


def test_retry_after_seconds_http_date():
    when = datetime.now(UTC) + timedelta(seconds=120)
    seconds = retry_after_seconds({"Retry-After": format_datetime(when, usegmt=True)}, 60)
    assert abs(seconds - 120) <= 1


def test_retry_after_seconds_http_date_in_past_floors_at_zero():
    when = datetime.now(UTC) - timedelta(seconds=120)
    assert retry_after_seconds({"Retry-After": format_datetime(when, usegmt=True)}, 60) == 0


def test_build_chat_request_basic():
    url, headers, body = build_chat_request(
        _CONFIG.base_url, _CONFIG.model, "the-key", [{"role": "user", "content": "hi"}]
    )
    assert url == "https://api.example.com/v1/chat/completions"
    assert headers["Authorization"] == "Bearer the-key"
    assert body["model"] == "gpt-4o"
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert "tools" not in body
    assert "stream" not in body


def test_build_chat_request_with_tools():
    tools = [{"type": "function", "function": {"name": "f"}}]
    _, _, body = build_chat_request(_CONFIG.base_url, _CONFIG.model, "k", [], tools=tools)
    assert body["tools"] == tools
    assert body["tool_choice"] == "auto"


def test_build_chat_request_stream_flag():
    _, _, body = build_chat_request(_CONFIG.base_url, _CONFIG.model, "k", [], stream=True)
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}


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


def test_parse_usage_drops_numbers_no_integer_column_can_hold():
    """`1e400` is valid JSON and decodes to `inf`; `int()` refuses it outright, so
    trusting it turned a good answer into a raw OverflowError for the caller."""
    data = json.loads('{"usage":{"prompt_tokens":1,"cost":1e400,"ratio":NaN,"why":"text"}}')
    u = parse_usage(data)
    assert u is not None
    assert u.prompt_tokens == 1
    assert u.extra is None


def test_parse_usage_drops_counts_past_a_64_bit_column():
    """The same hole one step further in: `1e400` is caught by `isfinite`, but a
    plain 30-digit integer is finite and still overflows the journal's column —
    the row is then lost on insert, silently, with the answer already returned."""
    data = json.loads('{"usage":{"prompt_tokens":' + "9" * 30 + ',"completion_tokens":4}}')
    u = parse_usage(data)
    assert u is not None
    assert u.prompt_tokens is None
    assert u.completion_tokens == 4


def test_parse_usage_keeps_the_largest_count_a_column_can_hold():
    data = {"usage": {"prompt_tokens": 2**63 - 1, "completion_tokens": 2**63}}
    u = parse_usage(data)
    assert u is not None
    assert u.prompt_tokens == 2**63 - 1
    assert u.completion_tokens is None


def test_parse_usage_keeps_a_good_answer_when_only_the_counts_are_junk():
    data = json.loads('{"choices":[{"message":{"content":"hi"}}],"usage":{"cost":1e400}}')
    content, tool_calls, usage = _parse_completion(data, "m")
    assert content == "hi"
    assert tool_calls is None
    assert usage is not None and usage.extra is None


def test_any_parse_failure_becomes_an_invalid_provider_response():
    """The backstop: enumerating exception types is what let `1e400` through, so
    whatever the next unmapped body raises must still surface as failover-able."""

    class Hostile(dict):
        def __getitem__(self, key):
            raise ValueError("provider body from hell")

    with pytest.raises(InvalidProviderResponseError) as exc_info:
        _parse_completion(Hostile(), "m")
    assert exc_info.value.model == "m"


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

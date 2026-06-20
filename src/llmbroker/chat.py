"""OpenAI-compatible chat primitives and the host-agnostic tool-loop helpers.

Request building, response parsing and retry parsing live here once, adapted to
the new ``LLMConfig`` (the resolved key is passed in, never read off the config).
``arun_tool_loop`` drives ``broker.chat()`` back-and-forth until a tool-call-free
reply; ``run_tool_loop`` is its sync wrapper.
"""

import json
from collections.abc import Callable, Mapping
from typing import Any

from llmbroker.models import LLMConfig, Usage

_CHAT_PATH = "/chat/completions"


def is_rate_limit(status_code: int) -> bool:
    return status_code in (429, 503)


def retry_after_seconds(headers: Mapping[str, str], default_sec: int) -> int:
    try:
        return int(headers.get("Retry-After", default_sec))
    except (ValueError, TypeError):
        return default_sec


def build_chat_request(
    config: LLMConfig,
    api_key: str,
    messages: list[dict],
    tools: list[dict] | None = None,
) -> tuple[str, dict[str, str], dict[str, Any]]:
    """Return (url, headers, json_body) for an OpenAI-compatible chat completion."""
    body: dict[str, Any] = {"model": config.model, "messages": messages}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    return (
        f"{config.base_url}{_CHAT_PATH}",
        {"Authorization": f"Bearer {api_key}"},
        body,
    )


def message_from_response(data: dict) -> dict:
    """Extract the assistant message object from a chat-completion response body."""
    return data["choices"][0]["message"]


def parse_usage(data: dict) -> Usage | None:
    """Extract token counts from a chat-completion response body, if present."""
    raw = data.get("usage")
    if not isinstance(raw, dict):
        return None
    known = {"prompt_tokens", "completion_tokens", "total_tokens"}
    extra = {
        k: int(v)
        for k, v in raw.items()
        if k not in known and isinstance(v, (int, float)) and float(v) == int(v)
    }
    return Usage(
        prompt_tokens=raw.get("prompt_tokens"),
        completion_tokens=raw.get("completion_tokens"),
        total_tokens=raw.get("total_tokens"),
        extra=extra or None,
    )


def parse_tool_calls(message: dict) -> list[dict] | None:
    """Extract the raw ``tool_calls`` list from an assistant message, verbatim."""
    tool_calls = message.get("tool_calls")
    if not tool_calls:
        return None
    return tool_calls


def execute_tool_calls(
    tool_calls: list[dict],
    dispatch: Mapping[str, Callable[..., object]],
) -> list[dict]:
    """Run each tool call via dispatch; return the tool-result messages to append."""
    results: list[dict] = []
    for call in tool_calls:
        name = call["function"]["name"]
        try:
            args = json.loads(call["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        if not isinstance(args, dict):
            args = {}
        fn = dispatch.get(name)
        if fn is None:
            output: object = f"Unknown tool {name}"
        else:
            try:
                output = fn(**args)
            except Exception as exc:  # noqa: BLE001 - report back to the model so it can retry
                output = f"Tool {name} failed: {exc}"
        results.append({"role": "tool", "tool_call_id": call.get("id"), "content": str(output)})
    return results


async def arun_tool_loop(
    llms,  # noqa: ANN001 - AsyncBroker (avoid import cycle)
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    dispatch: Mapping[str, Callable[..., object]] | None = None,
    max_steps: int = 8,
    **chat_kwargs,
) -> str:
    """Drive ``broker.chat`` until a tool-call-free reply; execute tools via dispatch."""
    convo = list(messages)
    dispatch = dispatch or {}
    for _ in range(max_steps):
        result = await llms.chat(convo, tools=tools, **chat_kwargs)
        if not result.tool_calls:
            return result.text
        convo.append(
            {"role": "assistant", "content": result.text or None, "tool_calls": result.tool_calls},
        )
        convo.extend(execute_tool_calls(result.tool_calls, dispatch))
    return ""


def run_tool_loop(
    llms,  # noqa: ANN001 - sync Broker
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    dispatch: Mapping[str, Callable[..., object]] | None = None,
    max_steps: int = 8,
    **chat_kwargs,
) -> str:
    """Synchronous tool loop over a sync ``Broker``.

    Mirrors ``arun_tool_loop`` but calls the blocking ``Broker.chat``; it does
    not use the async engine directly so it is safe to call from any thread.
    """
    convo = list(messages)
    dispatch = dispatch or {}
    for _ in range(max_steps):
        result = llms.chat(convo, tools=tools, **chat_kwargs)
        if not result.tool_calls:
            return result.text
        convo.append(
            {"role": "assistant", "content": result.text or None, "tool_calls": result.tool_calls},
        )
        convo.extend(execute_tool_calls(result.tool_calls, dispatch))
    return ""


__all__ = [
    "arun_tool_loop",
    "build_chat_request",
    "execute_tool_calls",
    "is_rate_limit",
    "message_from_response",
    "parse_tool_calls",
    "parse_usage",
    "retry_after_seconds",
    "run_tool_loop",
]

"""The tool loop, async and blocking: drive a broker's ``chat`` until it stops asking
for tools, running each requested tool through the host's dispatch. It sits above the
broker, not beside the HTTP primitives it never touches."""

import json
from collections.abc import Callable, Mapping

from llmbroker.broker.result import AsyncResult
from llmbroker.exceptions import ToolLoopLimitError
from llmbroker.sync import Result

_TOOL_LOOP_EXHAUSTED = (
    "the model still wanted tools after max_steps={max_steps} rounds —"
    " raise max_steps, or catch ToolLoopLimitError to keep the partial conversation"
)


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


def _advance_tool_loop(
    convo: list[dict],
    result: AsyncResult | Result,
    dispatch: Mapping[str, Callable[..., object]],
) -> bool:
    """Append the assistant turn and tool results; ``True`` once the reply is final."""
    if not result.tool_calls:
        return True
    convo.append(
        {"role": "assistant", "content": result.text or None, "tool_calls": result.tool_calls},
    )
    convo.extend(execute_tool_calls(result.tool_calls, dispatch))
    return False


async def arun_tool_loop(
    llms,  # noqa: ANN001 - AsyncBroker (avoid import cycle)
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    dispatch: Mapping[str, Callable[..., object]] | None = None,
    max_steps: int = 8,
    **chat_kwargs,
) -> AsyncResult:
    """Drive ``broker.chat`` until a tool-call-free reply; execute tools via dispatch.
    Returns that last round's result: earlier rounds are routed calls of their own,
    each with its own journal row, so ``usage`` is the final round's alone."""
    convo = list(messages)
    dispatch = dispatch or {}
    for _ in range(max_steps):
        result = await llms.chat(convo, tools=tools, **chat_kwargs)
        if _advance_tool_loop(convo, result, dispatch):
            return result
    raise ToolLoopLimitError(_TOOL_LOOP_EXHAUSTED.format(max_steps=max_steps))


def run_tool_loop(
    llms,  # noqa: ANN001 - sync Broker
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    dispatch: Mapping[str, Callable[..., object]] | None = None,
    max_steps: int = 8,
    **chat_kwargs,
) -> Result:
    """Synchronous tool loop over a sync ``Broker``, returning the final round's result.

    Mirrors ``arun_tool_loop`` but calls the blocking ``Broker.chat``; it does
    not use the async engine directly so it is safe to call from any thread.
    """
    convo = list(messages)
    dispatch = dispatch or {}
    for _ in range(max_steps):
        result = llms.chat(convo, tools=tools, **chat_kwargs)
        if _advance_tool_loop(convo, result, dispatch):
            return result
    raise ToolLoopLimitError(_TOOL_LOOP_EXHAUSTED.format(max_steps=max_steps))

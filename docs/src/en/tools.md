# Tools & agents

`run_tool_loop` drives the whole cycle: calls the model, executes the requested
tools via `dispatch` and repeats until a final reply with no tool calls.

```python
def get_weather(city: str) -> str:
    return f"It is 20°C in {city}"

tools = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Current weather in a city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}]

llms = llmbroker.Broker()
reply = llmbroker.run_tool_loop(
    llms,
    [{"role": "user", "content": "What is the weather in Paris?"}],
    tools=tools,
    dispatch={"get_weather": get_weather},
)
print(reply)
```

The async version is `await llmbroker.arun_tool_loop(...)` on top of
[`AsyncBroker`](async.md).

The loop is bounded by `max_steps` (8 by default). A model that still asks for
tools after the last round raises `llmbroker.ToolLoopLimitError` rather than
returning an empty answer — raise `max_steps`, or catch it to keep whatever the
conversation produced.

For manual control of the loop, `chat(messages, tools=...)` returns a result
with `tool_calls`.

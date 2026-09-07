# Tools & agents

`run_tool_loop` handles the complete tool-call cycle. It calls the model, runs
the requested functions from `dispatch`, and repeats until the model returns a
final reply with no further tool calls.

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

broker = llmbroker.Broker()
reply = llmbroker.run_tool_loop(
    broker,
    [{"role": "user", "content": "What is the weather in Paris?"}],
    tools=tools,
    dispatch={"get_weather": get_weather},
)
print(reply.text, "— by", reply.llm_name)
```

The function returns the final model call in the same format as `chat`: its text,
model name, and `usage`. Each earlier call has a separate journal row. The
returned `usage` therefore covers only the final call; calculate totals for the
whole loop from the journal.

With [`AsyncBroker`](async.md), use
`await llmbroker.arun_tool_loop(...)`.

`max_steps`, which defaults to 8, limits the number of model calls. If the model
requests another tool on the final step, the function raises
`llmbroker.ToolLoopLimitError`. Increase `max_steps` or handle the exception if
you want to retain the intermediate result.

## Parameters passed to the broker

Additional parameters, including `operation=`, `trace_id=`, and `wait=`, are
passed to every `chat` call in the loop. Set them as you would for a direct
`chat` call. Without `operation=`, every call uses the same general category, so
[quality ratings](usage.md#quality) cannot distinguish between tasks:

```python
reply = llmbroker.run_tool_loop(
    broker,
    messages,
    tools=tools,
    dispatch={"get_weather": get_weather},
    operation="weather-agent",
    trace_id=request_id,
)
```

Using one `trace_id` groups all calls in a loop in the journal. A rating recorded
with that ID applies only to the most recent successful call. To rate another
call in the same loop, keep its `call_id`. See
[Quality rating](usage.md#quality).

## Tool error handling

Exceptions raised by functions in `dispatch` do not propagate to the caller. If
a function fails, the model receives `Tool <name> failed: <error>` and may try
again with different arguments. If `dispatch` does not contain the requested
function, the model receives `Unknown tool <name>`. Successful function results
are also converted with `str()`, so tools should return text or a serialized JSON
string rather than an arbitrary object.

If a function converts an error into an ordinary result, the loop continues and
uses one step. To make a tool failure stop the request, handle it in the function
and return an unambiguous failure to the model, or implement the loop yourself.
`chat(messages, tools=...)` returns `tool_calls`, after which the application can
choose what to do.

## Limitation with scoped calls

The function's first argument is the broker itself. In a multi-user application,
`broker.for_scope(user)` works at runtime but does not match the declared argument
type, so a static type checker reports an error. The current tool-loop API does
not explicitly support scopes. See [Multi-user applications](server.md#multiuser).

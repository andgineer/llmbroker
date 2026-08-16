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

broker = llmbroker.Broker()
reply = llmbroker.run_tool_loop(
    broker,
    [{"role": "user", "content": "What is the weather in Paris?"}],
    tools=tools,
    dispatch={"get_weather": get_weather},
)
print(reply.text, "— by", reply.llm_name)
```

The loop returns the result of its final round, exactly like `chat`: the text,
the model that produced it, and that round's `usage`. Every earlier round was a
routed call of its own with its own journal row, so the counts here are the last
round's, not the whole loop's — read the rows if you want the total.

The async version is `await llmbroker.arun_tool_loop(...)` on top of
[`AsyncBroker`](async.md).

The loop is bounded by `max_steps` (8 by default). A model that still asks for
tools after the last round raises `llmbroker.ToolLoopLimitError` rather than
returning an empty answer — raise `max_steps`, or catch it to keep whatever the
conversation produced.

## What the loop passes to the broker

Anything else you pass goes into every `chat` the loop makes: `operation=`,
`trace_id=`, `wait=`. Set them as you would on an ordinary call — otherwise the
loop's rounds land in the common unlabelled bucket and
[quality rating](usage.md#quality) learns nothing from them:

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

One `trace_id` over the whole loop collects the task's rounds into a single
journal trace — useful, and intended. Rating by it needs care though: a loop is
several calls of its own, and [rating by trace](usage.md#quality) finds one of
them — the last round that answered. For another one, keep its `call_id`.

## Your tool's exception does not come back to you

`dispatch` is called by the loop, but its errors do not reach you: if the function
raises, the model gets back the text `Tool <name> failed: <error>` and decides
what to do with it — usually fixes the arguments and asks again. A model asking
for a tool that `dispatch` does not have gets `Unknown tool <name>`. What the
function returned comes back the same way: the result is coerced to a string, so
return text or JSON from a tool rather than an object.

Which also means what the loop will not notice: a tool that failed quietly costs a
step rather than stopping the loop. If a tool's failure should fail the request,
catch it inside your own function and hand the model an explicit refusal — or drive
the loop yourself: `chat(messages, tools=...)` returns a result with `tool_calls`,
and the rest is up to you.

## The loop takes a broker, not a caller

The first argument is the broker itself. In a multi-user service, where calls are
made by a [scoped caller](server.md#multiuser), passing `broker.for_scope(user)` to
the loop works but does not match the declared type — a type checker will complain
about it. While the loop takes a broker, there is no place for a scope here.

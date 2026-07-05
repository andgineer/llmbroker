# Async

`AsyncBroker` is the primary engine; `Broker` is its blocking wrapper. The
methods are the same, just with `await`:

```python
async with llmbroker.AsyncBroker("llms.toml") as llms:
    reply = await llms.ask("Hello")
    print(reply.text)
```

The async tool loop is `await llmbroker.arun_tool_loop(...)`, see
[Tools & agents](tools.md).

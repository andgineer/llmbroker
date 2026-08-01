# Async

`AsyncBroker` is the primary engine; `Broker` is its blocking wrapper. The
methods are the same, just with `await`:

```python
async with llmbroker.AsyncBroker("llms.toml") as llms:
    reply = await llms.ask("Hello")
    print(reply.text)
```

Streaming is async-only — the pool yields deltas as they arrive, with routing
and failover intact:

```python
    async for delta in llms.stream("Write a haiku about brokers"):
        print(delta, end="", flush=True)
```

See [Direct model calls](direct.md#streaming-from-the-pool) for what failover
can and cannot rescue mid-stream.

The async tool loop is `await llmbroker.arun_tool_loop(...)`, see
[Tools & agents](tools.md).

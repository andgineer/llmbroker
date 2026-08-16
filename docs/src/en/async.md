# Async

`AsyncBroker` is the primary engine; `Broker` is its blocking wrapper. The
methods are the same, just with `await`:

```python
async with llmbroker.AsyncBroker() as llms:
    reply = await llms.ask("Hello")
    print(reply.text)
```

Streaming is async-only — the pool yields deltas as they arrive, with routing
and failover intact:

```python
    stream = llms.stream("Write a haiku about brokers")
    async for delta in stream:
        print(delta, end="", flush=True)
    print(f"\n— by {stream.llm_name}, {stream.usage}")
```

`stream(...)` hands back a handle you iterate for the deltas; it also names the
model that answered and, once the answer is over, what it cost. Before the first
delta both are `None` — until then failover may still move the call.

See [Direct model calls](direct.md#streaming-from-the-pool) for what failover
can and cannot rescue mid-stream.

The async tool loop is `await llmbroker.arun_tool_loop(...)`, see
[Tools & agents](tools.md).

## One process, one file, no init step

For a single-process service, sqlite holds the models, the keys and the journal
in one file, and the curated model list fills it on the first call:

```python
async with llmbroker.AsyncBroker("broker.db") as llms:
    print((await llms.ask("Hello")).text)
```

The database starts empty and is filled before the pool is provisioned, so there
is no separate init step to remember, and it is kept current from then on. It is
best-effort: an unreachable catalog logs a warning and the process starts on
whatever the file already holds.

For several processes or hosts, fill the database once in the deploy job instead
— see [Servers & clusters](server.md#datasource).

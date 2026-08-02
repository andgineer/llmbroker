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

## One process, one file, no init step

For a single-process service, sqlite holds the models, the keys and the journal
in one file, and the curated lineup fills it on the first call:

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

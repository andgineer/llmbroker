# Async & streaming

`AsyncBroker` is the primary engine; `Broker` is its blocking wrapper. The
methods are the same, just with `await`:

```python
async with llmbroker.AsyncBroker() as llms:
    reply = await llms.ask("Hello")
    print(reply.text)
```

The async tool loop is `await llmbroker.arun_tool_loop(...)`, see
[Tools & agents](tools.md).

## Streaming {#streaming-from-the-pool}

Streaming is async-only — the pool yields deltas as they arrive, with routing
and failover intact:

```python
stream = llms.stream("Write a haiku about brokers", operation="write")
async for delta in stream:
    print(delta, end="", flush=True)

print(stream.llm_name, stream.usage)   # who answered, and what it cost
await stream.record_quality(0.9)       # rate it without naming the call yourself
```

`stream(...)` hands back a handle you iterate for the deltas; it also names the
model that answered and, once the answer is over, what it cost.

Failover works normally right up to the **first delta** — a rate-limited or
broken model is cooled down and the next one takes over, invisibly. Once text
has started arriving there is nothing left to fail over to, so a stream that
dies mid-answer raises `StreamInterruptedError`; the deltas you already received
stand.

That is also why the handle says nothing before the first delta: `llm_name` and
`call_id` are `None` until then, because the call may still move to another
model. `usage` fills in later still, when the answer is over.

Rating waits for the same moment the counts do — the end of the answer, when the
call reaches the journal. Ask earlier and you get a `ValueError` rather than a
score that quietly goes nowhere.

Stopping early is fine, but what hands the model's slot back is closing the
stream: a `break` does not close the handle by itself — abandoned, it frees the
slot only once Python collects it. Close it yourself; that is also the only way
to score what you did receive, since closing is what ends the call.

```python
stream = llms.stream("Write a haiku about brokers")
async for delta in stream:
    if looks_wrong(delta):
        break

await stream.aclose()
await stream.record_quality(0.0)
```

Or let a context manager close it — where there is nothing to score:

```python
async with contextlib.aclosing(llms.stream("...")) as stream:
    async for delta in stream:
        ...
```

Streaming one named model, with no pool and no failover, is what `direct` does —
see [Direct model calls](direct.md#streaming).

## One process, one file, no init step

sqlite holds the models, the keys and the journal in one file — enough for a
single-process service — and the curated model list fills it on the first call:

```python
async with llmbroker.AsyncBroker("broker.db") as llms:
    print((await llms.ask("Hello")).text)
```

The database starts empty and is filled before the pool is provisioned, so there
is no separate init step to remember, and it is kept current from then on. It is
best-effort: an unreachable catalog logs a warning and the process starts on
whatever the file already holds — and, where the file is empty, on the copy of
the preset shipped inside the package.

If that file is shared with your application, WAL and the file lock are worth
knowing about — see [SQLite: sharing and WAL](server.md#sqlite). For several
processes or hosts, fill the database once in the deploy job instead — see
[Servers & clusters](server.md#datasource).

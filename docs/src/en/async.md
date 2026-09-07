# Asynchronous calls and streaming

`AsyncBroker` provides the asynchronous API. `Broker` uses the same underlying
behavior through synchronous methods. Call `AsyncBroker` methods with `await`:

```python
async with llmbroker.AsyncBroker() as broker:
    reply = await broker.ask("Hello")
    print(reply.text)
```

Use `await llmbroker.arun_tool_loop(...)` for an asynchronous tool loop. See
[Tools & agents](tools.md).

## Streaming {#streaming-from-the-pool}

Streaming is available only through the asynchronous API. `stream()` yields text
chunks as they arrive. Model selection and fallback behave as they do for a
regular call:

```python
stream = broker.stream("Write a haiku about brokers", operation="write")
async for delta in stream:
    print(delta, end="", flush=True)

print(stream.llm_name, stream.usage)   # model name and usage information
await stream.record_quality(0.9)       # rate the completed reply
```

`stream(...)` returns an asynchronous iterator. After the reply is complete, its
fields contain the model name and usage information.

Before the first text chunk arrives, the broker can try another model. This
happens if the provider returns an error, rate-limits the request, or completes a
reply with neither text nor tool calls. Once a chunk has been returned, switching
models would require replacing visible text. If the reply is interrupted at that
point, `StreamInterruptedError` is raised and the application retains the chunks
it already received. Behavior with `fastest_of > 1` is described under
[Concurrent streaming](#racing-a-stream).

### Concurrent streaming {#racing-a-stream}

With `fastest_of > 1`, the broker calls multiple models concurrently. Every model
continues until one produces a **complete** reply. The model that returns the
first text chunk is not necessarily the one that finishes first, so the broker
does not cancel the other requests when text first appears.

Chunks received before one reply completes are provisional. If another model
finishes first, the iterator raises `StreamReplacementError`. Its `replacement`
field contains the complete final reply. Replace all previously displayed text
with that reply; chunks from different models are never combined.

```python
stream = broker.stream("Write a haiku", fastest_of=2, stream_selection_window=1.0)
parts = []
try:
    async for delta in stream:
        parts.append(delta)
        show(delta)
except llmbroker.StreamReplacementError as exc:
    replace_everything_with(exc.replacement.text)   # replace the provisional text
else:
    text = "".join(parts)

print(stream.llm_name)             # model whose reply became the final result
await stream.record_quality(0.9)   # rate the final reply
```

Rate a concurrent call through the `stream` object or `exc.replacement`. With
`fastest_of`, the journal can contain several successful attempts with the same
`trace_id`, although the user received only one reply.
`record_quality(..., trace_id=...)` cannot determine which reply was shown. Use
the returned object or save its `call_id`.

The iterator is finished after `StreamReplacementError` and yields no more data.
Catch this exception **before** the broader `LLMRequestError`, or a complete reply
will be handled as a failure.

`stream_selection_window` affects only the text shown first. For this many
seconds, one second by default, the broker waits for data from the model currently
first in the selection order. If it does not start responding, data already
received from another model is shown. If the first model fails, the wait ends
immediately. A value of `0` displays the first text received, regardless of model
order.

Expiration of `stream_selection_window` is not an error or a timeout and does not
change later model selection. The complete reply from the model that finishes
first is still the final result.

Each concurrent request consumes provider quota, and its reply remains in memory
until a result is selected. This is independent of how quickly the application
reads chunks. If one model stops generating or does not complete within `wait`,
the broker can use another model's complete reply. `StreamInterruptedError` or
`LLMTimeoutError` reaches the application only if no model completes.

When `fastest_of` is unset or equals `1`, the broker stays with the model that
returns the first chunk, and `stream_selection_window` has no effect.

### Time limit for the complete reply {#budget}

`wait` limits the time used to receive the complete reply, not only the first
chunk. It counts only time spent waiting for provider data:

```python
stream = broker.stream("Write a long answer", wait=20.0)
try:
    async for delta in stream:
        print(delta, end="", flush=True)
except llmbroker.LLMTimeoutError as exc:
    print(f"\nresponse timed out: {exc}")
```

Time spent by the application processing a chunk does not count. Timing pauses
when data are handed to the application and resumes when the next chunk is
requested. The limit still applies when a model starts quickly but generates the
rest of the reply too slowly.

When `wait` is unset, no user-defined time limit applies. If it expires before the
first chunk, the call raises `NoLLMAvailableError`. After the first chunk, it
raises `LLMTimeoutError`, and the application retains previously delivered data.

A model that returned no data within `wait` is temporarily excluded from
selection. If the model had already started responding, that temporary exclusion
does not apply. In both cases, the broker remembers which `wait` value was too
short and prefers another model for a similarly short call. An incomplete reply
cannot be rated; calling `record_quality` on its object raises an exception.

Before the first chunk, `llm_name` and `call_id` are `None` because the broker can
still try another model. `usage` is populated after the reply is complete.

A rating can also be recorded only after the reply is complete and the call is in
the journal. Calling `record_quality` earlier raises `ValueError`.

You can stop reading at any point, but `break` does not close the iterator or
release capacity for another request. Without explicit closure, this happens only
when Python collects the object. Call `aclose()` yourself. Closing also completes
the call and allows you to rate the partial reply:

```python
stream = broker.stream("Write a haiku about brokers")
async for delta in stream:
    if looks_wrong(delta):
        break

await stream.aclose()
await stream.record_quality(0.0)
```

If no rating is needed, use a context manager to ensure closure:

```python
async with contextlib.aclosing(broker.stream("...")) as stream:
    async for delta in stream:
        ...
```

Streaming is also available for one specific model through `direct()`, without
pool selection or fallback. See [Direct model calls](direct.md#streaming).

## A local database for one process

SQLite stores models, keys, and the journal in one file. This is sufficient for a
single-process application. The maintained model list is written before the
first call:

```python
async with llmbroker.AsyncBroker("broker.db") as broker:
    print((await broker.ask("Hello")).text)
```

An empty database is populated automatically before the pool is created, so no
separate initialization step is required. The list is then updated
automatically. If the remote catalog is unavailable, the broker logs a warning
and uses the data in the database. If the database is empty, it uses the copy
bundled with the installed package.

If the application uses the same file, account for WAL mode and SQLite file
locking. See [SQLite: sharing and WAL](server.md#sqlite). For multiple processes
or hosts, update the database once during deployment instead. See
[Servers & clusters](server.md#datasource).

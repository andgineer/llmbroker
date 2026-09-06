# Async & streaming

`AsyncBroker` is the primary engine; `Broker` is its blocking wrapper. The
methods are the same, just with `await`:

```python
async with llmbroker.AsyncBroker() as broker:
    reply = await broker.ask("Hello")
    print(reply.text)
```

The async tool loop is `await llmbroker.arun_tool_loop(...)`, see
[Tools & agents](tools.md).

## Streaming {#streaming-from-the-pool}

Streaming is async-only — the pool yields deltas as they arrive, with routing
and failover intact:

```python
stream = broker.stream("Write a haiku about brokers", operation="write")
async for delta in stream:
    print(delta, end="", flush=True)

print(stream.llm_name, stream.usage)   # who answered, and what it cost
await stream.record_quality(0.9)       # rate it without naming the call yourself
```

`stream(...)` hands back a handle you iterate for the deltas; it also names the
model that answered and, once the answer is over, what it cost.

Failover works normally right up to the **first delta** — a rate-limited or
broken model is cooled down and the next one takes over, invisibly. A model whose
answer ends without ever producing a delta is broken in that same sense: nothing
reached you, so it is failed over too rather than handed to you as an empty
stream. Once text has started arriving there is nothing left to fail over to, so a
stream that dies mid-answer raises `StreamInterruptedError`; the deltas you
already received stand. That last part is what changes when you race a stream with
`fastest_of` — see [Racing a stream](#racing-a-stream).

### Racing a stream {#racing-a-stream}

`fastest_of` on a stream does more than start two models: it keeps both running until
one has a *whole* answer. That is deliberate. The model that says its first word
soonest is often not the model that finishes soonest, and committing to it throws away
the only lane that could still rescue the call when it stalls or answers badly.

So the deltas you receive are provisional. If another model finishes first, the stream
stops and raises `StreamReplacementError` carrying that complete answer — you throw
away everything you have shown and use it instead. Nothing is ever spliced.

```python
stream = broker.stream("Write a haiku", fastest_of=2, stream_selection_window=1.0)
parts = []
try:
    async for delta in stream:
        parts.append(delta)
        show(delta)
except llmbroker.StreamReplacementError as exc:
    replace_everything_with(exc.replacement.text)   # exc.streamed_llm_name is discarded
else:
    text = "".join(parts)

print(stream.llm_name)             # whichever model's answer you ended up with
await stream.record_quality(0.9)   # rates that one, never the discarded lane
```

That handle — or `exc.replacement` — is also the only safe way to rate a race
later. `fastest_of` on `chat` or `ask` can leave two answered calls under one trace —
both models finished, and only one of them is the answer you got — so
`record_quality(..., trace_id=...)` has no way to tell which. Rate any race through
what it handed you, or keep the `call_id` off it.

The exception is terminal for that iterator: after it, no more deltas belong to the
stream. Catch it *before* a broad `LLMRequestError`, or a completed answer will be
handled as a failure.

`stream_selection_window` is what chooses which lane you see first, and nothing else.
For that many seconds — one by default — the pool's highest-ranked lane keeps the
right to be the visible one; if it starts inside that time you see it, otherwise you
see whatever a sibling has already produced. A lane that fails gives the right up
immediately rather than holding the interval out. `0` removes the preference: the
first text to arrive is the text you see. Expiry is not a timeout and teaches the pool
nothing — no model is cooled, set aside or ranked differently for missing it, and the
race itself is still decided purely on who finishes first.

The costs are worth stating plainly. Every lane is read to its end whatever your
reader is doing, so each live answer is held in memory until the race settles, and the
losers still spend their provider quota. In exchange, a model that goes quiet mid-answer
or dribbles past your budget no longer takes the call down with it: a sibling that
finished inside the budget replaces it. Only when no lane can finish does the failure
belonging to the text you saw — `StreamInterruptedError` or `LLMTimeoutError` — reach
you.

Without `fastest_of`, or with `fastest_of=1`, none of this applies: an ordinary stream
still commits at its first delta and the keyword changes nothing.

### The budget covers the whole answer {#budget}

`wait` bounds the answer, not its first token, and it counts only the time the
library spends waiting on the provider:

```python
stream = broker.stream("Write a long answer", wait=20.0)
try:
    async for delta in stream:
        print(delta, end="", flush=True)
except llmbroker.LLMTimeoutError as exc:
    print(f"\ngave up: {exc}")
```

Taking your time between deltas is yours to take: the clock is disarmed the moment
a delta is handed to you and picks up where it left off when you ask for the next
one, so a slow reader can never spend the budget. A model that opens at once and
then dribbles is therefore not inside a budget it is busy overrunning — which is
the whole point of bounding the answer rather than its opening.

Unset means unbounded, which is the default. Before the first delta an exhausted
budget ends the call with `NoLLMAvailableError`; after it, with `LLMTimeoutError`,
and the deltas already delivered stand. Where the difference shows is what it costs
the model: nothing at all by the deadline is silence, and the model is set aside
briefly like any other that failed you, while a model already writing when your
clock ran out is left alone — it answered, you simply stopped waiting. Both ways
the pool remembers the budget it did not finish within, so equally tight callers
are handed a sibling first. A call cut short this way cannot be rated: it never
settled, so `record_quality` on its handle raises.

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
stream = broker.stream("Write a haiku about brokers")
async for delta in stream:
    if looks_wrong(delta):
        break

await stream.aclose()
await stream.record_quality(0.0)
```

Or let a context manager close it — where there is nothing to score:

```python
async with contextlib.aclosing(broker.stream("...")) as stream:
    async for delta in stream:
        ...
```

Streaming one named model, with no pool and no failover, is what `direct` does —
see [Direct model calls](direct.md#streaming).

## One process, one file, no init step

sqlite holds the models, the keys and the journal in one file — enough for a
single-process service — and the curated model list fills it on the first call:

```python
async with llmbroker.AsyncBroker("broker.db") as broker:
    print((await broker.ask("Hello")).text)
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

# llmbroker

Combine many free, rate-limited LLMs into one reliable model — with no premium
subscription and no single point of failure. No heavyweight dependencies like
LangChain.

## Quick start

[Install llmbroker](installation.md). Free models are called with the providers'
API keys — which keys you need and where to get them, llmbroker tells you itself:

```bash
llmbroker env freetier > .env   # a .env skeleton: each key, and above it where to get it
```

Fill in whichever are easy to get — one is enough — and call:

```python
import llmbroker

broker = llmbroker.Broker()
print(broker.ask("Hello, how are you?").text)
```

That is the whole setup — no config file. `Broker()` brings up a curated pool of
free models, reads keys from the environment (and from a `.env` in the working
directory) and keeps the model list current by itself.

A key you never got breaks nothing: a model without one simply stays inactive.
And when a model hits its rate limit, the broker cools it down and moves to the
next — you get an answer rather than an error, for as long as one model is alive.

## What it can do

- **No configuration** — `Broker()` takes no arguments at all: the curated pool
  arrives on its own and [keeps itself fresh](usage.md#sync).
- **Automatic failover** — the broker works through the models until one answers;
  when nobody can, `NoLLMAvailableError` [says why](usage.md#errors).
- **Chat, tools and agents** — `ask`, multi-turn `chat`, [the whole tool loop in
  one function](tools.md).
- **Async and streaming** — [the same API with `await`](async.md), and the answer
  can be printed as the model writes it.
- **A paid model by name** — [`direct("opus")`](direct.md) alongside the free
  pool: an eternal alias instead of a version number, called directly, past the
  pool.
- **A pool that learns** — [rate the answers](usage.md#quality) and weak models
  drop to the back of the queue.
- **Keys anywhere** — the environment, `.env`, a DB, [AWS, Vault](secrets.md) or
  storage of your own; a key per user.
- **Scaling without code changes** — [a shared DB](server.md) across processes
  and hosts, [an endpoint of your own in the same
  routing](server.md#own-entry).
- **You can see what happens** — [pool snapshot, call journal and
  statistics](monitoring.md), tracing by your own `trace_id`, an alarm on
  degradation.
- **[Disabling models](disable.md)** by hand.

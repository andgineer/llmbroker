# llmbroker

Turn a crowd of free, rate-limited LLMs into one reliable model — no premium
subscription, no single point of failure. No heavy dependencies like LangChain.

## Quick start

[Install llmbroker](installation.md), then:

```bash
llmbroker preset freetier > llms.toml   # ready-made pool of free models
llmbroker env freetier > .env           # which keys you need, and where to get them
```

```python
llms = llmbroker.Broker("llms.toml")
print(llms.ask("Hello, how are you?").text)
```

Fill in whichever keys are easy to get: a model without a key simply stays
inactive. When a model hits its rate limit, the broker cools it down and switches
to the next one — you get an answer, not an error, as long as any model is up.

The curated preset keeps evolving. Refresh your file whenever you like — it is a
command, not code:

```bash
llmbroker preset freetier --sync llms.toml
```

Your `[[custom]]` models and your keys survive, and a model whose provider left
the preset stays in the file until a replacement you can actually call arrives —
so an update never leaves you with fewer working models than you had.

## Where to go next

| Your scenario | Read |
|---|---|
| **A simple script** | [Usage](usage.md): the pool, timeouts, quality rating |
| **FastAPI, agents, workers** | [Async](async.md): the same API with `await` |
| **Function calling** | [Tools & agents](tools.md): the whole tool loop in one call |
| **Secrets already in AWS or Vault** | [API keys](secrets.md): the broker reads them right from there |
| **Multiple instances, a shared DB** | [Servers & clusters](server.md): sqlite / Postgres / MongoDB, per-user keys |

## Features

- **Automatic failover** — an error only when no one is left at all
  (`NoLLMAvailableError`).
- **Chat, tools & agents** — `ask`, multi-turn `chat`, [tool calling](tools.md).
- **Async-first** — [`AsyncBroker`](async.md); `Broker` is a blocking wrapper
  around the same engine.
- **Self-learning pool** — [rate the replies](usage.md#quality), weak models sink
  to the back of the queue.
- **Keys anywhere** — environment, `.env`, DB, [AWS, Vault](secrets.md) or your
  own backend.
- **Scale out without code changes** — a [shared DB](server.md) across instances,
  a per-user key.
- **[Disabling models](disable.md)** manually, plus a pool state snapshot.

Full API reference — [Reference](reference.md).

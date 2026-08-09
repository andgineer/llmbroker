# llmbroker

Turn a crowd of free, rate-limited LLMs into one reliable model — no premium
subscription, no single point of failure. No heavy dependencies like LangChain.

## Quick start

[Install llmbroker](installation.md), get a key, and call:

```bash
llmbroker env freetier > .env   # which keys you need, and where to get them
```

```python
llms = llmbroker.Broker()
print(llms.ask("Hello, how are you?").text)
```

That is the whole setup — no config file. `Broker()` runs the curated pool of
free models, reads your keys from the environment (and the `.env` in your working
directory), and keeps the model list current by itself.

Fill in whichever keys are easy to get: a model without a key simply stays
inactive. When a model hits its rate limit, the broker cools it down and switches
to the next one — you get an answer, not an error, as long as any model is up.

Need a paid model too? Name it — it is reached directly and never joins the pool:

```python
llms = llmbroker.Broker(direct=["opus"])
llms.ask("Summarise this")               # the free pool, routed and learned
llms.direct("opus").ask("Now the hard part")   # Claude Opus, current version
```

Prefer your model list in a file you review? That still works — see
[Usage](usage.md#file).

## Where to go next

| Your scenario | Read |
|---|---|
| **A simple script** | [Usage](usage.md): the pool, timeouts, quality rating |
| **FastAPI, agents, workers** | [Async](async.md): the same API with `await` |
| **Function calling** | [Tools & agents](tools.md): the whole tool loop in one call |
| **Secrets already in AWS or Vault** | [API keys](secrets.md): the broker reads them right from there |
| **Multiple instances, a shared DB** | [Servers & clusters](server.md): sqlite / Postgres / MongoDB, per-user keys |

## Features

- **No configuration** — `Broker()` takes no arguments at all; the curated pool
  arrives and keeps itself current on its own.
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

# llmbroker

llmbroker presents several free language models through one interface. If one
model is unavailable or rate-limited, the broker tries another. It requires no
paid subscription or large dependency such as LangChain.

## Quick start

[Install llmbroker](installation.md). Free models require provider API keys. This
command creates a `.env` template and tells you where to obtain each key:

```bash
llmbroker env freetier > .env
```

Add at least one key, then create a broker and send a request:

```python
import llmbroker

broker = llmbroker.Broker()
print(broker.ask("Hello, how are you?").text)
```

No separate configuration file is required. `Broker()` loads the maintained list
of free models, reads keys from environment variables and `.env` in the working
directory, and updates the list automatically.

A model without a key is simply not used. If a provider rate-limits a request,
the broker tries the next available model. It returns an error only when no model
can answer.

## What it can do

- **No required configuration** — `Broker()` takes no arguments, and the free
  model list [updates automatically](usage.md#sync).
- **Automatic model fallback** — the broker tries models until one answers. If
  none can, the `reason` field on
  [`NoLLMAvailableError`](usage.md#errors) explains why.
- **Chat, tools, and agents** — `ask`, multi-turn `chat`, and a
  [complete tool-call loop](tools.md).
- **Async calls and streaming** — [an API based on `await`](async.md), with
  incremental output as the model generates it.
- **Direct paid-model calls** — [`direct("opus")`](direct.md) calls a model by a
  stable alias, independently of the pool.
- **Quality-based selection** — [reply ratings](usage.md#quality) affect the
  order used for later requests.
- **Keys anywhere** — the environment, `.env`, a DB, [AWS, Vault](secrets.md) or
  storage of your own; a key per user.
- **Scaling without code changes** — [a shared database](server.md) for multiple
  processes or hosts, plus [your own models in the pool](server.md#own-entry).
- **Operational visibility** — [pool state, a call journal, and
  statistics](monitoring.md), lookup by `trace_id`, and availability alerts.
- **[Disabling models](disable.md)** by hand.

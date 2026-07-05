# Usage

A synchronous script is the simplest scenario. For FastAPI and workers see
[Async](async.md).

## Model pool

Grab a ready-made pool of free LLMs and generate a `.env` with the keys:

```bash
llmbroker preset freetier > llms.toml
llmbroker env llms.toml > .env
```

`llmbroker env` prints a skeleton with a hint above each key — where to get it.
Fill in whichever keys are easy to get: a model without a key simply stays
inactive, it is not an error.

`llms.toml` is a plain TOML list of models; feel free to edit it and add your
own endpoints. For a provider that cannot handle parallel requests on one key,
set `parallel = 1` on its entry.

!!! tip "Keys do not have to live in `.env`"
    AWS Secrets Manager, Vault, a DB or your own storage — see [API keys](secrets.md).

## Calling the broker

```python
llms = llmbroker.Broker("llms.toml")

reply = llms.ask("Translate to French: Hello world")
print(reply.text)

# Full messages API
reply = llms.chat([
    {"role": "system", "content": "Answer briefly."},
    {"role": "user",   "content": "What is Python?"},
])
```

Limit how long to wait for a free model:

```python
try:
    reply = llms.ask("Question", wait=5.0)   # at most 5 seconds
except llmbroker.NoLLMAvailableError:
    print("All LLMs are busy")
```

`wait=0` fails immediately. Scripts do not need to close the broker; when you do
need to — see [Servers & clusters](server.md#closing).

## Quality rating {#quality}

Rate the replies and the broker learns which models are good at which tasks:

```python
reply = llms.ask("Summarize this contract clause", operation="summarize")
reply.record_quality(0.9)   # 1.0 — good reply, 0.0 — bad
```

Ratings accumulate per `(model, operation)` pair: a model consistently weak at a
given operation sinks to the back of the queue. Demotion is soft — if no other
models are left, it still answers — and it lifts with new good ratings; there is
no separate "reset". Calls without `operation=` share one common bucket.

Thresholds and the rating window are configurable — see
[`Optimizer`](reference.md#llmbroker.Optimizer).

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
inactive, it is not an error. The broker reads the `.env` sitting next to
`llms.toml` on its own; an exported variable wins over it.

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

Limit how long the whole call may take:

```python
try:
    reply = llms.ask("Question", wait=5.0)   # at most 5 seconds, start to finish
except llmbroker.NoLLMAvailableError:
    print("No LLM answered within the budget")
```

`wait` covers both halves of the call: waiting for a free model *and* the answer
itself. A provider still thinking when the budget runs out is abandoned — and is
not penalised for it, because the deadline was yours, not its fault. Without
`wait` a single attempt is bounded only by an internal 60-second ceiling.

A model that misses your budget does stop being the first choice for equally
tight budgets, so the next caller is handed a sibling instead of the same trap —
one call pays for the discovery, not all of them. Nothing is switched off:
callers with a roomier budget still get that model first, it is still used when
it is the only one left, and its next successful answer clears the mark.

`wait=0` is the one exception: it means "do not queue", not "answer instantly" —
every model that is free right now is tried, with no deadline of yours on the
answer. Scripts do not need to close the broker; when you do need to — see
[Servers & clusters](server.md#closing).

## Quality rating {#quality}

Rate the replies and the broker learns which models are good at which tasks:

```python
reply = llms.ask("Summarize this contract clause", operation="summarize")
reply.record_quality(0.9)   # 1.0 — good reply, 0.0 — bad; outside [0, 1] is a ValueError
```

Ratings accumulate per `(model, operation)` pair: a model consistently weak at a
given operation sinks to the back of the queue. Demotion is soft — if no other
models are left, it still answers — and it lifts with new good ratings; there is
no separate "reset". Calls without `operation=` share one common bucket.

**Rate it later.** The verdict often arrives long after the call — a user reviews
an LLM-produced artifact a day later. Persist `reply.llm_name` and the operation
you passed at call time, then record the rating whenever it arrives:

```python
# at call time, persist what you need
llm_name, operation = reply.llm_name, reply.operation

# ...a day later, when the user's review comes in
llms.record_quality(llm_name, operation, 0.0)
```

This folds into the same `(model, operation)` bucket as `reply.record_quality`;
the original call need not still exist — the rating is self-contained.

Thresholds and the rating window are configurable — see
[`Optimizer`](reference.md#llmbroker.Optimizer).

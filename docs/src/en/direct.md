# Direct model calls

The pool (`ask`/`chat`) routes over many models with failover. Sometimes you
want **one specific model**, called directly — a paid frontier model for a
quality task, or a single pool model you want to stream. That is what
`broker.direct(name)` gives you: a client for exactly that model, with **no
pool, no failover**, and — for the async client — **streaming**.

## Pooled vs direct

Every model lives in one registry. A per-entry `pool` flag (default `true`)
controls pool membership only:

- `pool = true` — part of the routed pool; a failover target for `ask`/`chat`.
- `pool = false` — kept in the registry but never routed. Reach it by name with
  `direct(...)`.

`broker.direct(name)` works for **any** entry — pooled or not. A paid model is
simply `pool = false`, so the pool never fails over onto it, yet it stays a
first-class model you can call directly.

## Configure a paid model

Start from the template:

```bash
llmbroker preset paid >> llms.toml   # appends a `pool = false` example
llmbroker env llms.toml >> .env      # adds the key line with a hint
```

```toml
[[llms]]
name        = "frontier"
base_url    = "https://api.anthropic.com/v1"   # any OpenAI-compatible endpoint
model       = "claude-opus-4-8"
api_key_ref = "ANTHROPIC_API_KEY"
pool        = false                            # excluded from failover; directable
```

`sync` mirrors only the config (`base_url` / `model` / `api_key_ref`) into the
DB — **never the key value**. The key is read from the env var or secrets
backend at call time, so updating a preset never touches your secret.

## Stream and ask (async)

```python
async with llmbroker.AsyncBroker("llms.toml") as llms:
    await llms.sync("llms.toml")   # mirror config into the registry/DB

    client = await llms.direct("frontier")

    # streaming — an async iterator of text deltas
    async for delta in client.stream("Write a haiku about brokers"):
        print(delta, end="", flush=True)

    # or the full reply at once
    result = await client.ask("Give me the full text")
    print(result.text, result.usage)
```

`direct(...)` also works on a **pool** model — the same API, without routing:

```python
free = await llms.direct("groq-llama-3.3-70b")
print((await free.ask("one specific model, no failover")).text)
```

## Synchronous

The blocking `Broker` offers `direct(...)` too, with `ask()` only (streaming is
async-only):

```python
with llmbroker.Broker("llms.toml") as llms:
    result = llms.direct("frontier").ask("...")
    print(result.text)
```

## Errors

Direct calls raise from one hierarchy under `LLMRequestError`:

- `UnknownModelError` — no registry entry matches the name.
- `MissingKeyError` — the model's `api_key_ref` is not set (a paid model without
  a key is an error here, unlike a pool model which just stays inactive).
- `ProviderError` — the provider returned an error, with `.status` and `.detail`.
  Catch it coarsely, or its subclasses `AuthError` (401/403) and `RateLimitError`
  (429/503, with `.retry_after`) for specific handling.
- `LLMTimeoutError` — the call exceeded its timeout.

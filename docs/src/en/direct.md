# Direct model calls

The pool (`ask`/`chat`) routes over many models with failover. Sometimes you
want **one specific model**, called directly — a paid frontier model for a
quality task, or a single pool model you want to stream. That is what
`broker.direct(name)` gives you: a client for exactly that model, with **no
pool, no failover**, and — for the async client — **streaming**.

## Two orthogonal flags

Every model lives in one registry. Two independent per-entry flags carry its
role:

- `pool` (default `true`) — pool membership. `pool = false` keeps the entry in
  the registry but out of routing; reach it by name with `direct(...)`.
- `custom` (default `false`) — provenance. Custom entries are yours, not part of
  the broker's curated preset, so `sync` never prunes them.

They are independent: a custom model may join the pool (`pool = true`) or stay
direct-only (`pool = false`). `broker.direct(name)` works for **any** entry.

## Add your own models

Put models you add under a `[[custom]]` array — the same fields as `[[llms]]`,
parsed by the same code, saved into the same registry, but flagged `custom`.

The quickest way is `add-model`, which picks from a curated catalog of paid
providers and appends the `[[custom]]` block for you:

```bash
llmbroker add-model --into llms.toml            # interactive: pick provider, then model
# or non-interactive:
llmbroker add-model --into llms.toml --provider anthropic --model claude-opus-4-8
```

It defaults to `pool = false` (direct-only); pass `--pool` to add it to the
pool instead. Then set the key it prints (`llmbroker env llms.toml >> .env`).

Or write the block by hand:

```toml
[[custom]]
name        = "frontier"
base_url    = "https://api.anthropic.com/v1"   # any OpenAI-compatible endpoint
model       = "claude-opus-4-8"
api_key_ref = "ANTHROPIC_API_KEY"
pool        = false                            # direct-only; reach via direct("frontier")
```

Either way, `llmbroker env llms.toml >> .env` adds the key line with a hint.

The file is the single source of truth: add a `[[custom]]` block to add a model,
remove it to remove one, then `sync` mirrors the whole file into the DB. `sync`
mirrors only the config (`base_url` / `model` / `api_key_ref`) — **never the key
value**; the key is read from the env var or secrets backend at call time.

## Refresh the pool without losing your models

Do **not** overwrite the file with `preset freetier > llms.toml` — that would
drop your `[[custom]]` block. Use `--merge` instead:

```bash
llmbroker preset freetier --merge llms.toml   # refresh [[llms]], keep [[custom]]
```

`--merge` rewrites the `[[llms]]` (preset-managed) entries and their `[keys]`
from the fresh preset while preserving your `[[custom]]` models and their keys.
Then `sync` as usual.

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
- `InvalidProviderResponseError` — HTTP 200 with a body that is not a chat
  completion (undecodable, or no assistant message), with `.model` and a
  `.detail` snippet. There is no failover here to hide it behind: the one model
  you named answered with garbage.
- `LLMTimeoutError` — the call exceeded its timeout.

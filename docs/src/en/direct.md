# Direct model calls

`ask`, `chat`, and `stream` select a model from the pool and try another after an
error. Use `broker.direct(...)` when you need **one specific model**. A direct
call does not select from the pool or automatically try another model.

Configure direct-access models with `direct=` when creating the broker. Access
pool models only through `ask`, `chat`, or `stream`. Passing a pool model name to
`direct()` raises `PoolModelError`.

## Configuration and use

```python
broker = llmbroker.Broker(direct=["opus"])
broker.direct("opus").ask("...")
```

`"opus"` is a stable alias from the maintained paid-model catalog. When a new
Claude generation becomes available, the catalog can associate `opus` with that
version without requiring an application change. Aliases do not contain version
numbers and are not renamed or removed. Configure the API key required by the
provider, such as `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`.

`llmbroker env freetier` does not list keys for paid models.

Models from `direct=` are not written to the registry; the application code
remains their source. Alias resolution is checked about once a day along with the
pool update. No separate synchronization or local file is required.

If the catalog is unavailable during a check, the broker keeps the currently
selected version and logs a warning. A known alias is not replaced with an older
version. Resolution can fail only on the first attempt, for example because of a
misspelled alias; the error lists the available aliases.

## A custom model configuration

Pass an `LLMConfig` instead of an alias for a self-hosted model, a company
gateway, or a version that must remain fixed:

```python
from llmbroker import LLMConfig

gateway = LLMConfig(
    name="frontier",
    model="claude-opus-4-8",
    base_url="https://api.anthropic.com/v1",   # any OpenAI-compatible endpoint
    api_key_ref="ANTHROPIC_API_KEY",
)
broker = llmbroker.Broker(direct=[gateway])
broker.direct(name="frontier").ask("...")
```

Updates to the catalog do not change this configuration or its model version.

## Direct models are not pool members

A model from `direct=` never participates in pool selection, is not used as a
fallback, and does not appear in `count()` or `snapshot()`. The pool is designed
for interchangeable free models with shared rate-limit behavior. Direct models
and private gateways are managed separately.

You can include your own model in the pool by [adding it to the
registry](server.md#own-entry). Automatic updates do not modify that entry. The
registry contains only pool models, so adding a model to the registry and
declaring it with `direct=` are separate choices.

## Finding a paid model

`llmbroker list` displays the maintained free and paid model lists without
changing anything. A `direct` line contains the alias for `direct=`, followed by
the provider ID, model ID, `base_url`, and `api_key_ref`:

```
$ llmbroker list
pool groq-gpt-oss-120b openai/gpt-oss-120b https://api.groq.com/openai/v1 GROQ_API_KEY
...
direct opus anthropic claude-opus-5 https://api.anthropic.com/v1 ANTHROPIC_API_KEY
direct sonnet anthropic claude-sonnet-5 https://api.anthropic.com/v1 ANTHROPIC_API_KEY
```

## Reading the catalog from a program {#curated}

You can read the same data programmatically without creating a broker. The
functions use the cached local copy first and the copy bundled with the installed
package if no cache exists. They do not access the network:

```python
from llmbroker import curated_paid, curated_pool, curated_providers

for row in curated_paid():
    print(row.alias or "-", row.name, row.label)

for provider in curated_providers():
    print(provider.id, provider.base_url, provider.api_key_ref)

print(len(curated_pool().configs), "free models in the maintained list")
```

You can also configure a model that is not yet in the catalog, such as a newly
released version. Create its configuration with `declare()` and pass it to
`direct=`:

```python
anthropic = next(p for p in curated_providers() if p.id == "anthropic")
broker = llmbroker.Broker(direct=[anthropic.declare("claude-opus-9-preview")])
broker.direct(name="anthropic-claude-opus-9-preview").ask("...")
```

`declare()` fills in the API base URL and key name. It returns a complete
configuration with a fixed model version, which is not updated with the catalog.
Catalog entries also have `.declare()`, and that method similarly fixes their
current model ID. Pass an alias string to `direct=` instead when you want future
version updates.

These functions only read data; `sync` performs updates. See [Errors](#errors)
for behavior when a key is missing.

## The difference between `alias` and `name`

Aliases and full model names are handled separately. You can use `name=` as a
version check: if you specify `anthropic-claude-opus-5` after the catalog alias
has moved to another version, the call fails rather than silently using the new
model.

Configurations from `direct=` are not stored in the registry. Key values are not
stored either; they are read from environment variables or the selected secrets
store at call time.

## Updating a model configured by alias

The broker checks an alias against the catalog about once a day along with the
pool update. When the alias moves, the old and new versions are logged:

```
direct=: opus: claude-opus-4-8 -> claude-opus-5
```

After the update, `name` contains the new model version. If the provider also
changes, the log entry includes the new `api_key_ref`; configure that environment
variable before the next call.

A complete `LLMConfig` is not updated because it is not associated with a catalog
entry.

## Asynchronous calls and streaming {#streaming}

```python
async with llmbroker.AsyncBroker(direct=["opus"]) as broker:
    client = await broker.direct("opus")

    # streaming through an asynchronous iterator
    async for delta in client.stream("Write a haiku about brokers"):
        print(delta, end="", flush=True)

    # or the full reply at once
    result = await client.ask("Give me the full text")
    print(result.text, result.usage)
```

A direct call uses the one model you selected. The broker does not select from the
pool, try another model after an error, or write a journal row. Pool streaming is
described in [Asynchronous calls](async.md#streaming-from-the-pool).

## Synchronous

Synchronous `Broker` also provides `direct(...)`, but only with `ask()`. Use
`AsyncBroker` for streaming.

```python
with llmbroker.Broker(direct=["opus"]) as broker:
    result = broker.direct("opus").ask("...")
    print(result.text)
```

## Request parameters {#params}

Direct calls can include any parameter supported by the selected model, such as
reasoning effort, temperature, a token limit, or `seed`. The `params` mapping is
added to the request body unchanged. Synchronous and asynchronous clients accept
it in `ask()`; the asynchronous client also accepts it in `stream()`:

```python
client = broker.direct("opus")
client.ask("...", params={"reasoning_effort": "low", "temperature": 0})
```

llmbroker does not validate or modify values in `params`. If the provider does
not support a parameter, its error response is returned to the application. Put
provider API parameters inside `params`; pass llmbroker parameters such as
`messages=` and `timeout=` separately.

llmbroker constructs `model`, `messages`, `stream`, `stream_options`, and `tools`
itself. Passing any of them in `params` raises `ValueError` naming the field. This
prevents replacement of the selected model or an incompatible change to the
response mode. `tool_choice` is allowed, so
`params={"tool_choice": "required"}` overrides the `"auto"` value set with
`tools`.

`params` is available only for direct calls. Pool calls accept parameters that
can be applied consistently to every model. See
[Schema-constrained output](usage.md#response-format).

## Errors {#errors}

All direct-call exceptions inherit `LLMRequestError`:

- `PoolModelError` — the name belongs to a model in the maintained pool. Use
  `ask`, `chat`, or `stream`, or create your own model configuration.
- `UnknownModelError` — no matching name or alias exists. If the value exists in
  the other category, the message explains that. The same error is raised at
  startup when `direct=` contains an alias not found in the paid catalog; the
  message lists available aliases.
- `MissingKeyError` — the key named by `api_key_ref` was not found. This is an
  error for a direct call; a pool model without a key is simply not used.
- `ProviderError` — the provider returned an error. `.status` and `.detail`
  contain the HTTP status and part of the response. For specific handling, catch
  `AuthError` (401 or 403) or `RateLimitError` (429 or 503, with
  `.retry_after`).
- `InvalidProviderResponseError` — the provider returned HTTP 200, but the body
  could not be parsed as a model reply or contained neither text nor tool calls.
  `.model` contains the model name and `.detail` contains part of the response.
  A direct call cannot try another model, so the exception reaches the
  application.
- `LLMTimeoutError` — the reply exceeded its time limit.

Pool streaming has two additional exceptions, `StreamInterruptedError` and
`StreamReplacementError`. They cannot occur during a direct call. See
[Asynchronous calls and streaming](async.md#streaming-from-the-pool).

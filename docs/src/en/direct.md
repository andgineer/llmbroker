# Direct model calls

The pool (`ask`/`chat`/`stream`) routes over many models with failover.
Sometimes you want **one specific model**, called directly — a paid frontier
model for a quality task. That is what `broker.direct(...)` gives you: a client
for exactly that model, with **no pool, no failover**.

Direct access is for **models you declare with `direct=`** where you build the
broker. Pool models are anonymous: reach them with `ask`/`chat`/`stream`, which
route and learn; naming one raises `PoolModelError`.

## Declare it and call it

```python
broker = llmbroker.Broker(direct=["opus"])
broker.direct("opus").ask("...")
```

`"opus"` is an **alias** from a curated catalog of paid providers — an eternal
handle. When the next Claude generation lands, llmbroker re-points `opus` at it
and your code does not change. An alias never disappears, never gets renamed, and
never carries a version number in it. Set the key it needs
(`llmbroker env freetier` does not list paid keys; the alias resolves
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and so on by provider).

Nothing is written anywhere for a declared model. The list in your code is the
only source of truth, and the alias is re-resolved against the catalog on the
same daily clock the pool refreshes on — which is what keeps it on the current
version with no sync and no file to update.

If the catalog cannot be reached when that clock comes round, the model stays on
the version it is already serving and a warning is logged. A resolution that
worked is never traded for an older one; only the very first one, at start-up,
can fail — that is where a mistyped alias tells you so, listing the ones that
exist.

## A model that is entirely yours

Pass a config instead of an alias — a self-hosted endpoint, a company gateway, a
version you must pin:

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

That one is yours down to the version: no refresh ever touches it, because
llmbroker was never told which catalog line it follows.

## The pool takes no model you declare here

Not "by default" — ever. A declared model is never routed, never failed over
onto, never a pool member in `count()` or `snapshot()`. The pool's whole value is
failover across interchangeable free endpoints curated as one set; a private
gateway dropped into it would be spilled onto by a rate limit that has nothing to
do with it, and would be handed traffic you meant for the free tier.

An endpoint of your own *can* be a pool member — by [writing it into your
registry](server.md#own-entry), where a refresh never touches it. That is a
decision you make once and record there, not a side effect of naming a model you
wanted to call. A registry holds pool members and nothing else, so putting a
model there is the opposite choice from declaring it here.

## Finding a paid model

`llmbroker list` prints both curated lists and writes nothing. A `direct` line
gives you the alias to declare, then the provider id, model id, `base_url` and
`api_key_ref` a pinned declaration states for itself:

```
$ llmbroker list
pool groq-gpt-oss-120b openai/gpt-oss-120b https://api.groq.com/openai/v1 GROQ_API_KEY
...
direct opus anthropic claude-opus-5 https://api.anthropic.com/v1 ANTHROPIC_API_KEY
direct sonnet anthropic claude-sonnet-5 https://api.anthropic.com/v1 ANTHROPIC_API_KEY
```

## `alias` and `name` are separate keyspaces

A call site says which one it means. That makes `direct(name=...)` a **version
assertion** as well as a lookup: point it at `anthropic-claude-opus-5` and the
day the catalog moves the alias onward, the call fails loudly instead of quietly
running a newer model.

Nothing you declare is written anywhere — no key value ever, and no config
either. The key is read from the env var or secrets backend at call time.

## Following the catalog without losing your version

A declared alias is re-resolved on the same daily clock the pool refreshes on.
When the catalog moves it, one line is logged naming both versions:

```
direct=: opus: claude-opus-4-8 -> claude-opus-5
```

A re-resolution gives the model a new `name` — that is what carries the version.
If it also moves to another provider, the line names the new `api_key_ref`: set
that env var before the next call.

A declaration you wrote out in full is never re-pointed: llmbroker was never told
which catalog line it follows.

## Stream and ask (async) {#streaming}

```python
async with llmbroker.AsyncBroker(direct=["opus"]) as broker:
    client = await broker.direct("opus")

    # streaming — an async iterator of text deltas
    async for delta in client.stream("Write a haiku about brokers"):
        print(delta, end="", flush=True)

    # or the full reply at once
    result = await client.ask("Give me the full text")
    print(result.text, result.usage)
```

This streams from the one model you named: no routing, no failover, no journal
row. Streaming over the pool is in [Async & streaming](async.md#streaming-from-the-pool).

## Synchronous

The blocking `Broker` offers `direct(...)` too, with `ask()` only (streaming is
async-only):

```python
with llmbroker.Broker(direct=["opus"]) as broker:
    result = broker.direct("opus").ask("...")
    print(result.text)
```

## Errors {#errors}

Direct calls raise from one hierarchy under `LLMRequestError`:

- `PoolModelError` — you named a preset-managed pool model. Use
  `ask`/`chat`/`stream`, or declare the model yourself.
- `UnknownModelError` — no entry matches. If your string exists in the *other*
  keyspace, the message says so. An alias in `direct=` that the paid catalog does
  not carry raises this at startup, listing the aliases it does.
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
- `StreamInterruptedError` — a **pool** stream died after deltas had already been
  emitted, with `.llm_name` and the cause attached.
- `StreamStalledError` — a **pool** stream went quiet for longer than the `stall`
  gap you set, with `.llm_name` and `.elapsed`. The model was still connected; it
  just stopped producing.

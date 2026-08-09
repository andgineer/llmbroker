# Direct model calls

The pool (`ask`/`chat`/`stream`) routes over many models with failover.
Sometimes you want **one specific model**, called directly — a paid frontier
model for a quality task. That is what `broker.direct(...)` gives you: a client
for exactly that model, with **no pool, no failover**.

Direct access is for **your own models** — declared with `direct=` in code, or
stored as `[[custom]]` in the lineup by `add-model`. Pool models are anonymous: reach them
with `ask`/`chat`/`stream`, which route and learn; naming one raises
`PoolModelError`.

## Declare it and call it

```python
llms = llmbroker.Broker(direct=["opus"])
llms.direct("opus").ask("...")
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
from llmbroker.models import LLMConfig

gateway = LLMConfig(
    name="frontier",
    model="claude-opus-4-8",
    base_url="https://api.anthropic.com/v1",   # any OpenAI-compatible endpoint
    api_key_ref="ANTHROPIC_API_KEY",
)
llms = llmbroker.Broker(direct=[gateway])
llms.direct(name="frontier").ask("...")
```

That one is yours down to the version: no refresh ever touches it, because
llmbroker was never told which catalog line it follows.

## The pool takes no model you declare here

Not "by default" — ever. A declared model is never routed, never failed over
onto, never a pool member in `count()` or `snapshot()`. The pool's whole value is
failover across interchangeable free endpoints curated as one set; a private
gateway dropped into it would be spilled onto by a rate limit that has nothing to
do with it, and would be handed traffic you meant for the free tier.

An endpoint of your own *can* be a pool member — by putting it in [your own
registry](usage.md#file), where a refresh never touches it. That is a decision
you make once and record there, not a side effect of naming a model you wanted to
call.

## In the lineup

The same two forms, written down. A `[[custom]]` array holds models that are
yours — the same fields as `[[llms]]`, parsed by the same code, but flagged
`custom` so a sync never prunes them and the router never reaches them.

### The lineup file is written for you

The file holding these entries is llmbroker's own: a refresh regenerates it in
full, and `add-model` is how anything gets into it. It lives in llmbroker's
directory — you never need its path — and its first line says as much. Comments
and keys llmbroker does not model do not survive a refresh, so it is not a place
to keep notes.

`add-model` picks from the paid catalog and writes the entry for you:

```bash
llmbroker add-model                             # interactive: pick provider, then model
# or non-interactive:
llmbroker add-model --provider anthropic --model claude-opus-4-8
```

It writes an alias entry: the `alias` you will call, plus a machine-formed `name`
carrying the current version. Then set the key it prints.

For the minority that must not move, `--pin` writes a name-only entry — no alias,
so no refresh ever touches it:

```bash
llmbroker add-model --pin --name frontier \
    --provider anthropic --model claude-opus-4-8
```

Either form lands as a `[[custom]]` entry:

```toml
[[custom]]
name = "frontier"
model = "claude-opus-4-8"
base_url = "https://api.anthropic.com/v1"
api_key_ref = "ANTHROPIC_API_KEY"
```

and you call it by name:

```python
client = await llms.direct(name="frontier")
```

`alias` and `name` are separate keyspaces, so a call site says which one it
means. That makes `direct(name=...)` a **version assertion** as well as a
lookup: point it at `anthropic-claude-opus-4-8` and the day a refresh moves the
alias onward, the call fails loudly instead of quietly running a newer model.

`add-model` prints the key line to set, with a hint for where to get it.

A stored entry carries only the config (`base_url` / `model` / `api_key_ref`) —
**never the key value**; the key is read from the env var or secrets backend at
call time. Your entries are yours: a refresh never prunes them and a curated
lineup can never overwrite them — one arriving with entries of its own is
rejected whole.

## Refresh without losing your models

A refresh happens by itself, and `broker.sync("freetier")` forces one. Either way
it does three things:

- rewrites the `[[llms]]` (preset-managed) entries and their `[keys]` from the
  fresh preset;
- keeps every model the preset dropped for which nothing usable arrived, so a
  refresh never costs you a working model;
- re-points every **alias** entry at what the paid catalog now recommends —
  `model`, `name`, `base_url` and `api_key_ref` — printing one line per change:

```
opus: claude-opus-4-8 -> claude-opus-5
```

A pinned entry — one with no alias — is never re-pointed, and an alias the
catalog no longer knows is a warning, not a rewrite.

A refreshed entry gets a new `name`, so its learned quality stats start clean —
what one version was good at says nothing about the next.

The new `name` is machine-formed the same way pool entries are named, so it can
occasionally land on one of them. The refresh refuses that outright and writes
nothing. Renaming your entry will not help — the next refresh forms the name
again — so drop its `alias` to pin it instead.

A refresh that moves an entry to another provider prints the new `api_key_ref`
too: set that env var before the next call.

## Stream and ask (async)

```python
async with llmbroker.AsyncBroker(direct=["opus"]) as llms:
    client = await llms.direct("opus")

    # streaming — an async iterator of text deltas
    async for delta in client.stream("Write a haiku about brokers"):
        print(delta, end="", flush=True)

    # or the full reply at once
    result = await client.ask("Give me the full text")
    print(result.text, result.usage)
```

## Streaming from the pool

You do not need `direct` to stream. The pool streams too, with routing and
failover intact:

```python
async for delta in llms.stream("Write a haiku about brokers", operation="write"):
    print(delta, end="", flush=True)
```

Failover works normally right up to the **first delta** — a rate-limited or
broken model is cooled down and the next one takes over, invisibly. Once text
has started arriving there is nothing left to fail over to, so a stream that
dies mid-answer raises `StreamInterruptedError`; the deltas you already received
stand. Pool streaming is async-only.

Stopping early is fine — `break` or an exception closes the iterator and hands
the model's slot back. If you instead keep the iterator in a variable and walk
away from it, close it yourself, or the slot stays busy until Python collects it:

```python
async with contextlib.aclosing(llms.stream("...")) as deltas:
    async for delta in deltas:
        ...
```

## Synchronous

The blocking `Broker` offers `direct(...)` too, with `ask()` only (streaming is
async-only):

```python
with llmbroker.Broker(direct=["opus"]) as llms:
    result = llms.direct("opus").ask("...")
    print(result.text)
```

## Errors

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

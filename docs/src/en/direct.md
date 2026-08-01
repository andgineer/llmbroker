# Direct model calls

The pool (`ask`/`chat`/`stream`) routes over many models with failover.
Sometimes you want **one specific model**, called directly — a paid frontier
model for a quality task. That is what `broker.direct(...)` gives you: a client
for exactly that model, with **no pool, no failover**.

Direct access is for **your own models**, the ones under `[[custom]]`. Pool
models are anonymous — reach them with `ask`/`chat`/`stream`, which route and
learn; naming one raises `PoolModelError`.

## Aliases: your code survives a model version bump

Ask for `"opus"`, not for `claude-opus-4-8`:

```python
client = await llms.direct("opus")
```

`opus` is an **alias** — an eternal handle. When the next Claude generation
lands, a catalog refresh re-points `opus` at it and your code does not change.
An alias never disappears, never gets renamed, and never carries a version
number in it.

## Two orthogonal flags

Every model lives in one registry. Two independent per-entry flags carry its
role:

- `pool` (default `true`) — pool membership. `pool = false` keeps the entry in
  the registry but out of routing.
- `custom` (default `false`) — provenance. Custom entries are yours, not part of
  the broker's curated preset, so `sync` never prunes them.

They are independent: a custom model may join the pool (`pool = true`) or stay
direct-only (`pool = false`). `direct(...)` works on any custom entry, pooled or
not.

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

It writes an alias entry: the `alias` you will call, plus a machine-formed
`name` carrying the current version. It defaults to `pool = false`
(direct-only); pass `--pool` to add it to the pool instead. Then set the key it
prints (`llmbroker env llms.toml >> .env`).

## Pin an exact version

For the minority that must not move, `--pin` writes a name-only block — no
alias, so no refresh ever touches it:

```bash
llmbroker add-model --into llms.toml --pin --name frontier \
    --provider anthropic --model claude-opus-4-8
```

Or write it by hand:

```toml
[[custom]]
name        = "frontier"
model       = "claude-opus-4-8"
base_url    = "https://api.anthropic.com/v1"   # any OpenAI-compatible endpoint
api_key_ref = "ANTHROPIC_API_KEY"
pool        = false                            # direct-only
```

and call it by name:

```python
client = await llms.direct(name="frontier")
```

`alias` and `name` are separate keyspaces, so a call site says which one it
means. That makes `direct(name=...)` a **version assertion** as well as a
lookup: point it at `anthropic-claude-opus-4-8` and the day a refresh moves the
alias onward, the call fails loudly instead of quietly running a newer model.

Either way, `llmbroker env llms.toml >> .env` adds the key line with a hint.

The file is the single source of truth: add a `[[custom]]` block to add a model,
remove it to remove one, then `sync` mirrors the whole file into the DB. `sync`
mirrors only the config (`base_url` / `model` / `api_key_ref`) — **never the key
value**; the key is read from the env var or secrets backend at call time.

## Refresh without losing your models

Do **not** overwrite the file with `preset freetier > llms.toml` — that would
drop your `[[custom]]` block. Use `--merge` instead:

```bash
llmbroker preset freetier --merge llms.toml
```

`--merge` does two things:

- rewrites the `[[llms]]` (preset-managed) entries and their `[keys]` from the
  fresh preset;
- re-points every **alias** entry at what the paid catalog now recommends —
  `model`, `name`, `base_url` and `api_key_ref` — printing one line per change:

```
opus: claude-opus-4-8 -> claude-opus-5
```

Pinned entries (no alias) are never touched, and an alias the catalog no longer
knows is a warning, not a rewrite. Then `sync` as usual.

A refreshed entry gets a new `name`, so its learned quality stats start clean —
what one version was good at says nothing about the next.

The new `name` is machine-formed the same way pool entries are named, so it can
occasionally land on one of them. `--merge` refuses that outright and writes
nothing — rename your entry, or drop its `alias` to stop refreshes renaming it.

## Stream and ask (async)

```python
async with llmbroker.AsyncBroker("llms.toml") as llms:
    await llms.sync("llms.toml")   # mirror config into the registry/DB

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
with llmbroker.Broker("llms.toml") as llms:
    result = llms.direct("opus").ask("...")
    print(result.text)
```

## Errors

Direct calls raise from one hierarchy under `LLMRequestError`:

- `PoolModelError` — you named a preset-managed pool model. Use
  `ask`/`chat`/`stream`, or add a `[[custom]]` entry for it.
- `UnknownModelError` — no entry matches. If your string exists in the *other*
  keyspace, the message says so.
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

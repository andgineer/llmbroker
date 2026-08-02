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

### Which model is tried first {#weight}

Row order does not decide it — `weight` does:

```toml
[[llms]]
name        = "google-gemini-3.5-flash-lite"
base_url    = "https://generativelanguage.googleapis.com/v1beta/openai"
model       = "gemini-3.5-flash-lite"
api_key_ref = "GEMINI_API_KEY"
weight      = 0.75
```

A weight is a number from 0 to 1 — how good you expect this model's answers to
be, on the same scale as the ratings you record. Higher goes first. The default
is `0.0`, so an entry you add without one is tried after every weighted model:
give your own entries a weight if you want them competing on merit.

It is a starting point, not a fixed order. Every rating you record through
[`record_quality()`](#quality) moves the model off its weight and toward what it
actually earns, and once you have rated it enough times the weight stops counting
altogether — the order is then whatever your ratings say, however you ranked the
models to begin with. A model nobody has rated yet still starts where you put it,
instead of at the bottom where it could never earn its way up.

!!! tip "Keys do not have to live in `.env`"
    AWS Secrets Manager, Vault, a DB or your own storage — see [API keys](secrets.md).

### Keeping the pool fresh {#sync}

Providers come and go, and the curated preset follows them. Refreshing your file
is one command:

```bash
llmbroker preset freetier --sync llms.toml
```

It rewrites the managed models and their key hints, keeps your `[[custom]]`
entries, and prints a report of what it did. From code the same operation is
`llms.sync("freetier")`, and it returns that report:

```python
report = llms.sync("freetier")       # a preset name — the only call that goes online
print(report)                        # or forward it to your own admin channel
```

You rarely need to. **The curated lineup keeps itself current on its own**, with
no argument and no job to schedule: providers retire free endpoints without
notice, so a lineup that stops updating slowly stops working. The broker
re-checks it about once a day, lazily — the check happens on a call you were
making anyway, never on a timer, so an idle process does nothing at all.

It is best-effort: if the catalog is unreachable, the broker logs a warning and
carries on with the config it already has. The explicit `llms.sync(...)` call
raises instead — you asked for it, so you get to handle it.

**A check that changes nothing touches nothing.** Your `llms.toml` is rewritten
only when the curated lineup genuinely moved, so a file under version control
stays byte-identical — and untouched mtime — on every check that found no news.

To follow your own lineup instead of ours, name it; to follow nothing, say so:

```python
llmbroker.Broker("llms.toml", sync="my-lineup.toml")   # yours, kept current
llmbroker.Broker("llms.toml", sync=None)               # nothing is refreshed
llmbroker.Broker("llms.toml", sync_interval=3600)      # check hourly instead
```

### Where llmbroker keeps its own state

Outside your config, llmbroker caches a little of its own: the fetched preset, the
paid catalog, and when it last checked for an update. That lives in one machine
directory — `~/Library/Caches/llmbroker` on macOS, `$XDG_CACHE_HOME/llmbroker` on
Linux, `%LOCALAPPDATA%\llmbroker` on Windows. Point it elsewhere with
`$LLMBROKER_HOME`, or per broker with `home=`, which is how two projects on one
machine keep entirely separate state. Nothing in there is authoritative: delete
it, or run where nothing is writable, and the broker still works — it just
re-fetches.

A `.toml` config is synced from a curated preset only. To roll a vendored config
out to a database registry instead, see
[Servers & clusters](server.md).

Four things in the report are worth understanding:

- **A pending key** is a model waiting for a key you have not set. Harmless: it
  stays inactive and the pool routes over the rest. The report prints where to
  get the key.
- **A kept entry** is a model whose provider left the curated lineup while you
  still hold a key for it. Nothing happens to it: it keeps routing exactly as
  before, and it disappears by itself once it stops working.

  ```
  kept: openrouter-nemotron — the lineup no longer carries OPENROUTER_API_KEY
  and this installation has a key for it, so it stays
  ```
- **A retired entry** is that same model after your own call journal proved it
  dead — at least one 401/403/404 and not one success. A bad week of 429s and
  5xx proves nothing and changes nothing. Removing an entry from your config is
  the one destructive thing a sync does, so the line shows the evidence:

  ```
  retired: groq-llama-3.3-70b — 401 since 2026-07-02, no successful call since;
  the lineup dropped it too
  ```
- **An unused key** is a key you actually have that nothing in your config
  references any more. Whether to revoke it at the provider is your call, and a
  `[[custom]]` entry still using it keeps it out of that advice. A provider you
  never had a key for just disappears quietly — there is nothing to revoke.

That is the whole rule: a sync never takes away a model you can call, unless the
same provider replaces it or your journal says it does not work.

### Watching the pool {#pool-health}

One call answers "is this pool healthy?" — per-model facts and the pool-wide
picture come off the same object:

```python
snap = llms.snapshot()

print(f"{snap.providers_usable} of {snap.providers_total} providers usable")
if snap.degraded:
    print("one quota left — nothing to fail over to")

for key in snap.missing_keys:
    print(f"{key.api_key_ref} holds back {', '.join(key.entry_names)}")
    print(key.help)                      # where to get it

for name, llm in snap.items():           # still a mapping of name -> per-model facts
    print(name, llm.has_key, llm.cooldown_until)
```

The unit is the provider, not the model: two entries sharing one API key are one
quota and count once. A pool with one usable provider is **degraded** — it can
still answer, but a rate limit has nowhere to spill. Revoke or rotate a key and
the count follows on the next reconcile, so the numbers never lag behind your
keys. Models you disabled yourself still count their provider; that verdict is on
the per-model rows above.

llmbroker logs the same verdict, so you can alert on it without polling. Both
lines are `ERROR`, and you get one each time the state changes — including
falling from the first to the second:

```
pool degraded, no failover left: 1 of 3 providers usable — no key for GEMINI_API_KEY, GROQ_API_KEY
pool cannot serve any request: no provider has a key — no key for ...
```

A recovery logs one `INFO` (`pool recovered: 3 of 3 providers usable`); gaining a
further provider after that is not worth a line. Missing keys on their own are
never an alarm — two providers may be all you want.

## Calling the broker {#calling}

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

To print the answer as it is written rather than all at once, the pool streams
too — `async for delta in llms.stream(...)`, async-only, see
[Async](async.md).

## Quality rating {#quality}

Rate the replies and the broker learns which models are good at which tasks:

```python
reply = llms.ask("Summarize this contract clause", operation="summarize")
reply.record_quality(0.9)   # 1.0 — good reply, 0.0 — bad; outside [0, 1] is a ValueError
```

Ratings accumulate per `(model, operation)` pair: a model consistently weak at a
given operation sinks to the back of the queue, displacing the [weight](#weight)
it started from as they add up. Demotion is soft — if no other
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

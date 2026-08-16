# Usage

A synchronous script is the simplest scenario. For FastAPI and workers see
[Async](async.md).

## Model pool

The pool is the curated list of free LLMs, and you get it by asking for a broker:

```python
llms = llmbroker.Broker()
```

There is nothing to create and nothing to keep. Generate the key skeleton and
fill in whichever keys are easy to get:

```bash
llmbroker env freetier > .env
```

`llmbroker env` prints a hint above each key — where to get it. A model without a
key simply stays inactive, it is not an error. The broker reads the `.env` in your
working directory; an exported variable wins over it.

A provider that cannot handle parallel requests on one key is capped by
`parallel` on its entry. For the pool that is the curated model list's call, not
yours — the file is llmbroker's, see [below](#file); on a model of your own you
set it, as a field of the `LLMConfig` you declare.

### Your own paid models {#direct}

Nothing you declare here ever joins the pool: what the pool sells is failover
across interchangeable free endpoints, and your company gateway has no business
being spilled onto by someone else's 429 unless you put it there
[on purpose](#file). Declared models are reached by name instead:

```python
llms = llmbroker.Broker(direct=["opus"])
llms.direct("opus").ask("...")
```

`"opus"` is an alias from a curated catalog of paid models — an eternal handle
that llmbroker keeps pointing at the current generation. Pass an `LLMConfig`
instead to declare an endpoint of your own, exactly as written. Either way
nothing is stored: the declaration in your code is the only source of truth.
See [Direct model calls](direct.md).

### Where the model list lives {#file}

You do not have to put it anywhere. A broker keeps its model list in llmbroker's own
directory — `~/.cache/llmbroker` on Linux, `~/Library/Caches/llmbroker` on macOS
— and refreshes it there. Set `LLMBROKER_HOME` to move that directory, which is
what a container without a writable cache needs.

That file is written by llmbroker, not by you: a refresh regenerates it in full,
and it holds the pool and nothing else — a model you reach by name is
[declared in code](direct.md). There is no way to point a broker at a model list file
of your own — a model list arrives as a curated preset name and nothing else. What
you can name is a database:

```python
llms = llmbroker.Broker("postgresql://…")   # registry, keys and journal, all three
```

See [Servers & clusters](server.md). To supply the pool yourself, pass an object
implementing the registry protocol and say what it follows:

```python
llms = llmbroker.Broker(registry=MyRegistry(), sync=None)        # only your entries
llms = llmbroker.Broker(registry=MyRegistry(), sync="freetier")  # yours plus ours
```

| you pass | `sync=` left out | entries you put there yourself |
|---|---|---|
| nothing, or a database URL | follows `"freetier"` | never touched by a refresh |
| a registry object | an error — say which | never touched by a refresh |

A refresh only ever rewrites what a sync itself wrote, so "the curated free pool
plus two endpoints of my own, routed together" is just a registry with both in
it.

#### An entry of your own, in the pool {#own-entry}

You put your own entry there through the registry protocol, not by writing rows:
the table layout is llmbroker's own and may change between releases. Read what is
there, add yours, write it all back — `mirror` is a total mirror, so anything you
leave out is deleted:

```python
from llmbroker.models import LLMConfig
from llmbroker.postgres import Registry

registry = Registry(pool)
mine = LLMConfig(
    name="my-gateway",
    base_url="https://gw.internal/v1",
    model="m",
    api_key_ref="MY_GATEWAY_KEY",
)
await registry.mirror([*await registry.load(), mine])
```

Nothing marks it as ours, so no sync ever removes or rewrites it. It is a pool
member and the router fails over onto it — an endpoint you want reached by name
instead is [a declared model](direct.md), which is not stored at all.

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

Providers come and go, and the curated preset follows them. You do not have to do
anything about it. When you want to force a refresh, it is one call, and it
returns a report of what it did:

```python
report = llms.sync("freetier")           # a preset name — the only call that goes online
print(llmbroker.format_report(report))   # or forward the report to your own admin channel
```

You rarely need to. **The curated model list keeps itself current on its own**, with
no argument and no job to schedule: providers retire free endpoints without
notice, so a model list that stops updating slowly stops working. The broker
re-checks it about once a day, lazily — the check happens on a call you were
making anyway, never on a timer, so an idle process does nothing at all.

It is best-effort: if the catalog is unreachable, the broker logs a warning and
carries on with the config it already has. The explicit `llms.sync(...)` call
raises instead — you asked for it, so you get to handle it.

**A check that changes nothing touches nothing.** The model list is rewritten only
when the curated one genuinely moved, so a check that found no news leaves it
byte-identical, mtime included.

To follow nothing — because you fill the registry yourself — say so, and the
check interval is yours to set:

```python
llmbroker.Broker(sync=None)              # nothing is refreshed
llmbroker.Broker(sync_interval=3600)     # check hourly instead
llmbroker.Broker(sync_interval=None)     # never check by itself — you run the sync
```

`sync_interval=None` is for a process that may make no outbound connection while it
serves: it stops every automatic fetch, including the one that fills an empty
registry at startup, and the freshness becomes yours to keep — see
[Servers & clusters](server.md#no-fetch).

### Where llmbroker keeps its own state

llmbroker keeps a little of its own: the fetched preset, the paid catalog, when it
last checked for an update — and, when you named no database, the model list it
runs and its call journal too. That lives in one machine directory —
`~/Library/Caches/llmbroker` on macOS, `$XDG_CACHE_HOME/llmbroker` on Linux,
`%LOCALAPPDATA%\llmbroker` on Windows. Point it elsewhere with `$LLMBROKER_HOME`,
or per broker with `home=`, which is how two projects on one machine keep entirely
separate state.

None of it is authoritative: delete it, or run where nothing is writable — a
read-only container — and the broker still works. It re-fetches, and in the
read-only case simply remembers nothing between runs. Even with no network at all,
a first run starts on the copy of the preset shipped inside the package.

The one thing that does need a real directory is a refresh, which exists to leave
a copy behind: with nowhere writable it fails and says so — make one writable, or
run with `sync_interval=None` and fetch nothing by yourself.

Sharing one journal per machine is deliberate in the zero-config case: your keys
come from the environment, so the rate limits it remembers really are one pool,
and scattering the journal per working directory would make every run rediscover
the same 429. Pass `home=` if you want a project to keep its own.

To keep a database registry current from your own deploy job, see
[Servers & clusters](server.md).

Three things in the report are worth understanding:

- **A pending key** is a model waiting for a key you have not set. Harmless: it
  stays inactive and the pool routes over the rest. The report prints where to
  get the key.
- **A removed entry** is a model the curated list no longer carries. It goes,
  whether or not you still hold a key for it — the list is what decides which
  models this pool routes over, and a model leaves it only once it can no longer
  be called. Nothing is lost if it comes back later: the key stays in the secrets
  store and everything learned about the model derives from your call journal.
- **An unused key** is a key you actually have that nothing in your config
  references any more. Whether to revoke it at the provider is your call, and a
  model of your own still using it keeps it out of that advice. A provider you
  never had a key for just disappears quietly — there is nothing to revoke.

A removal is never silent: taking a provider away is what moves the usable-provider
count, and the pool alarm below fires on the way down to one and to none.

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

for key in snap.direct_missing_keys:     # your own models, reached by name
    print(f"{key.api_key_ref} — direct({key.entry_names[0]!r}) will fail")
    print(key.help)

for name, llm in snap.items():           # still a mapping of name -> per-model facts
    print(name, llm.has_key, llm.cooldown_until)
```

`direct_missing_keys` is separate from `missing_keys` on purpose: a model you
declared is never routed, so a key it lacks cannot degrade the pool and the pool
gaining a provider cannot fix it. Both carry the same `help` — from your own
`[keys]` block if you wrote one, otherwise from the curated catalog.

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
llms = llmbroker.Broker()

reply = llms.ask("Translate to French: Hello world")
print(reply.text)

# Full messages API
reply = llms.chat([
    {"role": "system", "content": "Answer briefly."},
    {"role": "user",   "content": "What is Python?"},
])
```

Every call also takes `trace_id=` — your own request or job id, stored on the
journal rows the call leaves behind and never interpreted, so that the journal
lines up with your logs. See [Tracing one request](server.md#trace).

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
an LLM-produced artifact a day later. A rating names the call it rates, and there
are two ways to name it. Pass an id of your own as `trace_id=` at call time and
rate by it later:

```python
llms.ask("Summarize this clause", operation="summarize", trace_id=document_id)

# ...a day later, when the user's review comes in
llms.record_quality(0.0, trace_id=document_id)
```

Or persist `reply.call_id` and rate that one attempt: `llms.record_quality(0.0,
call_id=saved_call_id)`. Exactly one of the two is required.

A key is the way to rate a call you no longer hold. If you do still hold it —
including the handle a [stream](direct.md#streaming-from-the-pool) hands back —
its own `record_quality(...)` needs no key and no journal read. On a stream it
becomes available once the answer is over, not while it is still arriving.

The model and the operation are read off the call, so you store neither. One
`trace_id` covering several calls rates them all, and the attempts that failed —
a model that rate-limited before another answered — are not rated: there was no
answer to judge. If nothing under the key answered, or retention has already
purged it, you get an `UnknownCallError` rather than a rating that quietly went
nowhere.

Rate through the same caller that made the call: the scope comes from the caller
object, not from the key, so a scoped call is rated with
`broker.for_scope(user).record_quality(...)` — sent through the bare broker it
would land unscoped.

Thresholds and the rating window are configurable — see
[`Optimizer`](reference.md#llmbroker.Optimizer).

# Model pool and calls

## Model pool

The pool is the curated list of free LLMs, and you get it by asking for a broker:

```python
broker = llmbroker.Broker()
```

There is nothing to create and nothing to keep. Generate the key skeleton and
fill in whichever keys are easy to get:

```bash
llmbroker env freetier > .env
```

`llmbroker env` prints a hint above each key — where to get it, see
[CLI](cli.md#env). A model without a key simply stays inactive, it is not an
error.

A provider that cannot handle parallel requests on one key is capped by
`parallel` on its entry. For the pool that is the curated model list's call, not
yours — the file is llmbroker's, see [below](#file); on a model of your own you
set it, as a field of the [`LLMConfig`](reference.md#llmbroker.models.LLMConfig)
you declare.

A paid model can be reached by name: `Broker(direct=["opus"])`, then
`broker.direct("opus").ask(...)`. It is called directly and never joins the pool —
see [Direct model calls](direct.md).

### Where the model list lives {#file}

You do not have to put it anywhere. A broker keeps its model list in [llmbroker's
own directory](#state) and refreshes it there. Set `LLMBROKER_HOME` to move that
directory, which is what a container without a writable cache needs.

That file is written by llmbroker, not by you: a refresh regenerates it in full,
and it holds the pool and nothing else — a model you reach by name is
[declared in code](direct.md). There is no way to point a broker at a model list file
of your own — a model list arrives as a curated preset name and nothing else.

To keep the list in a database instead, shared across processes, or to fill the
registry yourself — see [Servers & clusters](server.md#datasource).

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
give [your own entries](server.md#own-entry) a weight if you want them competing
on merit.

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
report = broker.sync("freetier")         # a preset name — the only call that goes online
print(llmbroker.format_report(report))   # or forward the report to your own admin channel
```

You rarely need to. **The curated model list keeps itself current on its own**, with
no argument and no job to schedule: providers retire free endpoints without
notice, so a model list that stops updating slowly stops working. The broker
re-checks it about once a day, lazily — the check happens on a call you were
making anyway, never on a timer, so an idle process does nothing at all.

It is best-effort: if the catalog is unreachable, the broker logs a warning and
carries on with the config it already has. The explicit `broker.sync(...)` call
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

**The sync report.** Its fields are in
[`SyncReport`](reference.md#llmbroker.models.SyncReport), and three things in it
are worth understanding:

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
count, and the [pool alarm](monitoring.md#alerts) fires on the way down to one
and to none.

### Where llmbroker keeps its own state {#state}

llmbroker keeps a little of its own: the fetched preset, the paid catalog, when it
last checked for an update — and, when you named no database, the model list it
runs and its call journal too. That lives in one machine directory, and which
directory it is follows this order, down to the first one it can write to:

1. `home=`, this broker's own, if you passed it;
2. `$LLMBROKER_HOME`, this process's own, if the variable is set;
3. `$XDG_CACHE_HOME/llmbroker`, and without that variable the platform cache:
   `~/Library/Caches/llmbroker` on macOS, `~/.cache/llmbroker` on Linux,
   `%LOCALAPPDATA%\llmbroker` on Windows;
4. a per-user directory under the system temp — the last thing left.

So `$XDG_CACHE_HOME` overrides the platform cache, and `home=` and
`$LLMBROKER_HOME` override that in turn: which is how two projects on one machine
keep entirely separate state.

Whatever happens to that directory, the broker will not break: delete it, or run
where nothing is writable, and it still works. It re-fetches, and where no
candidate directory is writable — the temporary one included — the state lives in
memory for that run alone. Even with no network at all, a first run starts on the
copy of the preset shipped inside the package.

That is harmless for what was fetched only. **The call journal, and with it
everything the pool learned, cannot be recovered from anywhere.** With no database
the journal lives in that same directory, so deleting it erases the call history
and the [quality ratings](#quality) built on it for good — the models restart from
their curated [weights](#weight), as on a first run. Keep the journal in a
database if you want the history to survive — see
[Servers & clusters](server.md#datasource).

The one thing that does need a real directory is a refresh, which exists to leave
a copy behind: with nowhere writable it fails and says so — make one writable, or
run with `sync_interval=None` and fetch nothing by yourself.

Sharing one journal per machine is deliberate in the zero-config case: your keys
come from the environment, so the rate limits it remembers really are one pool,
and scattering the journal per working directory would make every run rediscover
the same 429. Pass `home=` if you want a project to keep its own.

To keep a database registry current from your own deploy job, see
[Servers & clusters](server.md).

## Calling the broker {#calling}

```python
broker = llmbroker.Broker()

reply = broker.ask("Translate to French: Hello world")
print(reply.text)

# Full messages API
reply = broker.chat([
    {"role": "system", "content": "Answer briefly."},
    {"role": "user",   "content": "What is Python?"},
])
```

Every call also takes `trace_id=` — your own request or job id, stored on the
journal rows the call leaves behind and never interpreted, so that the journal
lines up with your logs. See [Tracing one request](monitoring.md#trace).

To print the answer as it is written rather than all at once, the pool streams
too — `async for delta in broker.stream(...)`, async-only, see
[Streaming](async.md#streaming-from-the-pool).

Scripts do not need to close the broker; when you do need to — see
[Servers & clusters](server.md#closing).

### How long to wait for an answer {#wait}

```python
try:
    reply = broker.ask("Question", wait=5.0)   # at most 5 seconds, start to finish
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
answer.

### When nobody can answer {#errors}

The pool works through the models until one answers. A model that returns HTTP 200
carrying neither text nor tool calls has not answered — it is failed over like any
other broken reply, so a reply that says nothing at all never reaches you as a
success. If nobody answered, the call raises `NoLLMAvailableError`, and you do not
have to read the message: the reason is in the fields.

```python
try:
    reply = broker.ask("Question", wait=5.0)
except llmbroker.NoLLMAvailableError as exc:
    if exc.retry_at is not None:
        retry_after(exc.retry_at)          # somebody comes back by then, on its own
    else:
        alert(f"the pool is not serving: {exc.reason}")
```

`reason` is a short string telling five unlike situations apart:

| `reason` | what happened | what to do |
|---|---|---|
| `empty_pool` | the registry holds no entries at all | fill it — see [Keeping the pool fresh](#sync) and [Servers & clusters](server.md#sync) |
| `no_keys` | there are entries, but this caller can pay for none of them | set the keys — see [API keys](secrets.md) |
| `all_disabled` | every model is [disabled by hand](disable.md) | enable at least one |
| `timeout` | your `wait` ran out — queueing for a free model, or already on the answer | retry with a larger budget, or later |
| `excluded` | every candidate dropped out on this particular request — the provider rejected each one's key, say | read the call journal: the reason per attempt is there |

The first three mean "this installation is not configured", and a human fixes
them, not a retry. The last two are about one request, and the next one may pass.

`retry_at` is filled only where a model is known to come back by itself: it is the
moment the nearest cooling model's cooldown expires. `empty_pool`, `no_keys` and
`all_disabled` carry none — there is nothing to wait for; nor does `timeout` when
nothing is cooling, where retrying in a second is no better than retrying now.

**An error in the request itself is not a `NoLLMAvailableError`.** If every model
tried answered "this request is wrong" (a 4xx other than 401/403/429 — a 400 on a
malformed `tools` schema, say), that says nothing about the models: what comes up
is a `ProviderError` carrying `.status` and `.detail`, the code and a snippet of
the body, which is the only thing you can act on. It is the same class
[direct calls](direct.md#errors) raise, so one `except` can cover both:

```python
try:
    reply = broker.ask(prompt, wait=5.0)
except llmbroker.NoLLMAvailableError as exc:
    ...                                    # nobody to answer — see above
except llmbroker.ProviderError as exc:
    log.error("every model rejected the request: HTTP %s — %s", exc.status, exc.detail)
```

Nothing is cooled down by it — the next, corrected request reaches those models as
usual.

## Quality rating {#quality}

Rate the replies and the broker learns which models are good at which tasks:

```python
reply = broker.ask("Summarize this contract clause", operation="summarize")
reply.record_quality(0.9)   # 1.0 — good reply, 0.0 — bad; outside [0, 1] is a ValueError
```

Ratings accumulate per `(model, operation)` pair: a model consistently weak at a
given operation sinks to the back of the queue, displacing the [weight](#weight)
it started from as they add up. Demotion is soft — if no other
models are left, it still answers — and it lifts with new good ratings; there is
no separate "reset". Calls without `operation=` share one common bucket.

**Rate it later.** The verdict often arrives after the call — a user reviews an
LLM-produced artifact the next day. A rating names the call it rates, and there
are two ways to name it. Pass an id of your own as `trace_id=` at call time and
rate by it later:

```python
broker.ask("Summarize this clause", operation="summarize", trace_id=document_id)

# ...a day later, when the user's review comes in
broker.record_quality(0.0, trace_id=document_id)
```

Or persist `reply.call_id` and rate that one attempt: `broker.record_quality(0.0,
call_id=saved_call_id)`. Exactly one of the two is required.

A key is the way to rate a call you no longer hold. If you do still hold it —
including the handle a [stream](async.md#streaming-from-the-pool) hands back —
its own `record_quality(...)` needs no key and no journal read. On a stream it
becomes available once the answer is over, not while it is still arriving.

The model and the operation are read off the call, so you store neither. The
attempts that failed — a model that rate-limited before another answered — are not
rated: there was no answer to judge, and the rating goes to the attempt that
answered.

**One rating, one call, and the search goes back a week.** Two bounds worth
knowing in advance:

- **A `trace_id` identifies one call.** llmbroker will not stop you putting one on
  several — the journal groups them exactly as you would expect — but a rating
  names exactly one call, and that will be the newest one that answered under the
  trace. If the trace turns out to carry noticeably more rows than a single call
  does, llmbroker also warns about it in the log. To rate one specific call out of
  several, keep its `call_id`: that is what it is for.
- **The call is looked for among the last 7 days.** Rating an older one is
  pointless: the quality window is rebuilt from a recent journal tail, and such a
  verdict would not survive the next pool rebuild. So rather than working through a
  quarter of journal for a vanishing effect, llmbroker refuses:
  `UnknownCallError`. You get the same one when the key matched nothing at all, or
  when no attempt answered — a rating never disappears silently.

All of that is about rating **by key**. Rating through the call itself — a `reply`
or a stream handle — looks nothing up and is bounded by no window.

Rate through the same caller that made the call: the scope comes from the caller
object, not from the key, so a scoped call is rated with
`broker.for_scope(user).record_quality(...)` — sent through the bare broker it
would land unscoped.

Thresholds and the rating window are configurable — see
[`Optimizer`](reference.md#llmbroker.Optimizer).

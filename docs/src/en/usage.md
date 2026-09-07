# Model pool and calls

## Model pool

The pool is a maintained list of free language models. Create a broker to use it:

```python
broker = llmbroker.Broker()
```

You do not need to assemble or maintain the list. Create a file for provider API
keys and add the keys you want to use:

```bash
llmbroker env freetier > .env
```

`llmbroker env` tells you where to obtain each key. See [CLI](cli.md#env). A
model without a key is simply not used.

If a provider limits concurrent requests on one key, the `parallel` setting
accounts for that limit. Values are already set for models in the maintained
list. For a model you add yourself, set `parallel` in its
[`LLMConfig`](reference.md#llmbroker.models.LLMConfig).

You can configure a paid model for direct calls with
`Broker(direct=["opus"])`, then call it with
`broker.direct("opus").ask(...)`. It is not part of the pool. See
[Direct model calls](direct.md).

### Where the model list is stored {#file}

The broker stores the list in [llmbroker's data directory](#state) and updates it
there. Set `LLMBROKER_HOME` to use another directory. This is useful in a
container that cannot write to the system cache directory.

Each update regenerates the file in full, so do not edit it. The file contains
only the pool. Models used for direct calls are [declared in code](direct.md).
You cannot pass an arbitrary model-list file to the broker; select one of the
maintained lists by name instead.

See [Servers & clusters](server.md#datasource) to store the list in a shared
database or manage the registry yourself.

### Which model is tried first {#weight}

Row order does not matter. The `weight` setting determines the initial order:

```toml
[[llms]]
name        = "google-gemini-3.5-flash-lite"
base_url    = "https://generativelanguage.googleapis.com/v1beta/openai"
model       = "gemini-3.5-flash-lite"
api_key_ref = "GEMINI_API_KEY"
weight      = 0.75
```

`weight` is the expected answer quality on a scale from 0 to 1. A higher value
makes the broker try the model earlier. The default is `0.0`, so models without
an explicit weight are tried after models with positive weights. Set a weight
when you [add your own model](server.md#own-entry).

The weight controls only the initial order. Ratings recorded with
[`record_quality()`](#quality) gradually replace it with measured quality. Once
there are enough ratings, the original weight is no longer used. Until then, an
unrated model keeps the position established by its weight.

!!! tip "Keys do not have to live in `.env`"
    AWS Secrets Manager, Vault, a DB or your own storage — see [API keys](secrets.md).

### Updating the model list {#sync}

Available free models change over time. llmbroker tracks these changes and
normally requires no manual update. To update immediately, call `sync`; the
method returns a report:

```python
report = broker.sync("freetier")         # freetier is the model-list name
print(llmbroker.format_report(report))   # log the report or send it to an administrator
```

By default, the broker checks for updates about once a day during an ordinary
call. No scheduled job is required, and an idle process performs no checks.

If an automatic update fails, the broker logs a warning and continues with the
existing configuration. An explicit `broker.sync(...)` reports the failure to
the caller as an exception.

If the maintained list has not changed, the local file is not rewritten. Its
contents and modification time remain unchanged.

You can disable automatic updates or change the check interval:

```python
llmbroker.Broker(sync=None)              # do not use a maintained model list
llmbroker.Broker(sync_interval=3600)     # check once an hour
llmbroker.Broker(sync_interval=None)     # do not check automatically
```

`sync_interval=None` disables every automatic download, including the initial
population of an empty registry. In this mode, run updates yourself. See
[Servers & clusters](server.md#no-fetch).

The report fields are documented in
[`SyncReport`](reference.md#llmbroker.models.SyncReport). A report can include
these states:

- **Pending key** — the model has no configured API key. The model is not used,
  but the rest of the pool remains available. The report tells you where to
  obtain the key.
- **Removed entry** — the model is no longer in the maintained list and is
  removed from the pool. Its key remains in the secrets store, and its call
  history remains in the journal. Those data are available if the model returns.
- **Unused key** — the secrets store contains a key that no model in the current
  configuration references. If your own configuration does not need it, you can
  revoke it at the provider.

Removing a model affects the number of usable providers. An
[availability alert](monitoring.md#alerts) is emitted when that count reaches one
or zero.

### Where llmbroker keeps its own state {#state}

llmbroker stores the downloaded model list, the paid-model catalog, and the time
of the last update check. If no database is configured, the active registry and
call journal are stored there as well. The directory is selected in this order:

1. the `home=` argument, if set;
2. `$LLMBROKER_HOME`, if set;
3. `$XDG_CACHE_HOME/llmbroker`, and without that variable the platform cache:
   `~/Library/Caches/llmbroker` on macOS, `~/.cache/llmbroker` on Linux,
   `%LOCALAPPDATA%\llmbroker` on Windows;
4. a per-user directory inside the system temporary directory.

`home=` has the highest priority, followed by `$LLMBROKER_HOME` and then
`$XDG_CACHE_HOME`. Use `home=` or `$LLMBROKER_HOME` to keep projects on the same
machine separate.

This order determines where data is written. Write access is not required for
reading: if `home=` points to a read-only directory, such as one mounted into a
container, the broker uses data from that directory instead of searching for a
writable alternative.

If the directory is missing, the broker downloads the data again. If no candidate
directory is writable, including the temporary directory, data remains in memory
until the process exits. Even without network access, a first run can use the
copy bundled with the package.

The downloaded list can be restored, but **the call journal and accumulated
quality data cannot**. Without a database, the journal is kept in the same
directory. Deleting it makes models start again from their initial
[weights](#weight). Store the journal in a database if the history must persist.
See [Servers & clusters](server.md#datasource).

An update requires a writable directory because the downloaded list must be
saved. If no directory is writable, the update fails. Provide write access or
disable automatic downloads with `sync_interval=None`.

Without a database, llmbroker uses one journal per machine. Processes that share
environment keys can therefore reuse knowledge about provider limits instead of
discovering the same limit independently. Pass `home=` to keep a separate
journal for one project.

See [Servers & clusters](server.md) to update a database registry from a
deployment job.

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

Every call accepts `trace_id=`, an identifier from your application such as a
request or job ID. llmbroker stores it unchanged so journal entries can be
matched to your logs. See [Finding the entries for one request](monitoring.md#trace).

To receive an answer incrementally, use the asynchronous `broker.stream(...)`
method with `async for`. See [Streaming](async.md#streaming-from-the-pool).

Ordinary scripts do not need to close the broker. See
[Servers & clusters](server.md#closing) for shutdown in server applications.

### How long to wait for an answer {#wait}

```python
try:
    reply = broker.ask("Question", wait=5.0)   # at most 5 seconds, start to finish
except llmbroker.NoLLMAvailableError:
    print("No model answered within the requested time")
```

`wait` limits the total duration of the call, including both the wait for an
available model and the response itself. If a provider returns no data during
that time, the broker stops the attempt and temporarily avoids that provider for
requests with the same short limit. This prevents every subsequent call from
waiting for the same slow provider. Without `wait`, a single attempt is limited
only by an internal 60-second maximum.

A model that misses the requested time also stops being the first choice for
calls with the same or a smaller `wait`. It remains available for calls with a
longer limit and when no other model is available. Its next timely response
clears this restriction.

`wait=0` has a special meaning: do not wait for a model to become available. The
broker considers models that are available immediately but does not set a
deadline on their responses.

### Calling several models concurrently {#parallel}

```python
reply = broker.ask("Question", fastest_of=2)   # call two models and return the first reply
```

`fastest_of=N` sends the request to at most `N` different models concurrently and
returns the first complete reply. Every request consumes provider quota even when
its reply is discarded. The option is disabled by default and is appropriate
when latency matters more than request count. If fewer than `N` models are
available, the broker uses all available models. `wait` applies to the whole call
and is not multiplied by the number of models.

The broker also makes one additional concurrent request automatically. After an
error, a model is temporarily excluded from selection. When that period ends,
the broker checks the model again while also calling another available model.
The first complete reply is returned, so rechecking the unavailable model does
not add latency. In other cases, a normal call sends one request to one model.

```python
reply = broker.ask("Question", parallel_recovery=False)  # do not send the extra request
```

Set `parallel_recovery=False` when request count matters more than latency. The
broker then checks the previously unavailable model first and moves to the next
model only if that attempt also fails.

When `fastest_of` is not set, `broker.stream(...)` uses the first model that
starts returning text and stays with that model for the rest of the response. To
call multiple models concurrently, as with `ask(..., fastest_of=N)`, pass
`fastest_of`:

```python
stream = broker.stream("Question", fastest_of=2, stream_selection_window=1.0)
```

With `fastest_of > 1`, every selected model continues generating a full reply.
`stream_selection_window` specifies how many seconds to wait for the broker's
first-choice model before showing data that another model has already returned.
The default is `1.0`; `0` displays data from the first model to respond. This
setting controls only which text is shown initially. The final result is still
the complete reply from the model that finishes first.

Concurrent streaming uses more memory because the broker keeps each selected
model's reply until the first one is complete. Text shown before that point is
provisional. If another model finishes first, the broker raises
`StreamReplacementError`, and the application must replace all previously shown
text. Replies from different models are never combined. See
[Concurrent streaming](async.md#racing-a-stream) for an example.

`fastest_of` and `stream_selection_window` apply only to the pool. A direct call
through `direct()` uses one specific model, so neither option applies.

### Asking for JSON that matches a schema {#response-format}

```python
reply = broker.ask(
    "Give me the card for the word 'tenacious'",
    operation="card",
    response_format={
        "type": "json_schema",
        "json_schema": {"name": "card", "schema": MY_SCHEMA, "strict": True},
    },
)
```

`response_format` is passed to the selected model unchanged. Synchronous and
asynchronous clients accept it in `ask` and `chat`; the asynchronous client also
accepts it in `stream`. The value uses the provider's OpenAI-compatible format.
llmbroker does not inspect it.

**Schema compliance is not guaranteed.** Some models consistently follow a
strict schema, while others accept the parameter but return another shape.
llmbroker does not validate reply content, so the application must validate it
against the schema.

You can record the validation result as a [quality rating](#quality) and set an
`operation=` for the task. Models that often violate the schema will then be
selected later for that operation. `response_format` is preferable to requesting
JSON only in the prompt: models that support the parameter can enforce a strict
schema, while behavior does not become worse for the others.

A reply that does not match the schema is still successful from llmbroker's
perspective. The broker does not temporarily exclude the model or send the same
request to another model. Results from testing the free model list are recorded
in `specs/reference/freetier-providers.md`.

This limitation applies to the pool. A specific model called through
[`direct()`](direct.md#params) accepts arbitrary request parameters.

### When nobody can answer {#errors}

The broker tries models until it receives a reply. If a provider returns HTTP 200
but the response contains neither text nor tool calls, the attempt is treated as
failed and the broker tries the next model. If no model answers, the call raises
`NoLLMAvailableError`. Inspect its fields rather than parsing the error message.

```python
try:
    reply = broker.ask("Question", wait=5.0)
except llmbroker.NoLLMAvailableError as exc:
    if exc.retry_at is not None:
        retry_after(exc.retry_at)          # a model will be available by this time
    else:
        alert(f"the pool is unavailable: {exc.reason}")
```

The `reason` field has one of five values:

| `reason` | what happened | what to do |
|---|---|---|
| `empty_pool` | the registry contains no models | populate it — see [Updating the model list](#sync) and [Servers & clusters](server.md#sync) |
| `no_keys` | models exist, but their API keys are unavailable for this call | configure keys — see [API keys](secrets.md) |
| `all_disabled` | every model is [disabled by hand](disable.md) | enable at least one |
| `timeout` | `wait` expired while waiting for an available model or a reply | increase `wait` or retry later |
| `excluded` | no model can be used for this request; for example, providers rejected every available key | inspect individual attempts in the call journal |

The first three values indicate a configuration problem, so retrying without a
configuration change will not help. `timeout` and `excluded` apply to one request;
the next request may succeed.

`retry_at` is set when no model is currently available but the broker knows when
one model's temporary exclusion ends. It is not set for `empty_pool`, `no_keys`,
or `all_disabled`, because waiting cannot resolve those conditions. For
`timeout`, it is set only when every model is temporarily unavailable. If a
model is already free, the request can be retried immediately.

**An invalid request does not produce `NoLLMAvailableError`.** If every selected
model rejects the request with a 4xx response other than 401, 403, or 429, the
broker raises `ProviderError`. A malformed `tools` schema is one example. The
`.status` field contains the HTTP status and `.detail` contains part of the
provider response. [Direct model calls](direct.md#errors) use the same exception:

```python
try:
    reply = broker.ask(prompt, wait=5.0)
except llmbroker.NoLLMAvailableError as exc:
    ...                                    # no model is currently available
except llmbroker.ProviderError as exc:
    log.error("Every model rejected the request: HTTP %s — %s", exc.status, exc.detail)
```

`ProviderError` does not exclude any model. A corrected request reaches those
models in the normal order.

## Quality rating {#quality}

Ratings help the broker select the best models for different tasks:

```python
reply = broker.ask("Summarize this contract clause", operation="summarize")
reply.record_quality(0.9)   # 1.0 — good reply, 0.0 — bad; outside [0, 1] is a ValueError
```

Ratings accumulate separately for each `(model, operation)` pair. A model with
low ratings for an operation is selected later but remains available when no
other model can answer. New positive ratings move it forward again; no separate
reset is required. As ratings accumulate, they replace the initial
[weight](#weight). Calls without `operation=` share one general category.

You can rate a reply later, after a user has reviewed the result. The rating must
identify the call it applies to. One option is to pass your own `trace_id=` when
making the call and use it when recording the rating:

```python
broker.ask("Summarize this clause", operation="summarize", trace_id=document_id)

# ...a day later, when the user's review comes in
broker.record_quality(0.0, trace_id=document_id)
```

Alternatively, save `reply.call_id` and rate that specific call with
`broker.record_quality(0.0, call_id=saved_call_id)`. Provide exactly one of
`trace_id` and `call_id`.

An identifier is needed when the result object is no longer available. If you
still have the result, call its own `record_quality(...)` method without a journal
lookup. This also applies to the object returned by
[streaming](async.md#streaming-from-the-pool); its method becomes available after
the response is complete.

The model and operation are read from the call record, so they do not need to be
stored separately. Failed attempts are not rated; the rating applies only to the
model whose reply was returned.

Two restrictions apply when rating by identifier:

- **One `trace_id` should identify one call.** llmbroker allows the same
  `trace_id` on multiple calls, but a rating is applied to the most recent
  successful call with that ID. If the journal contains substantially more
  matching rows than one call normally creates, llmbroker logs a warning. Save a
  call's `call_id` when you need to identify it unambiguously.

  With `fastest_of`, one call can leave several successful journal rows: one for
  every model that completed before the result was selected. The rows do not
  indicate which reply the user received. Rate such a call through its returned
  result object rather than `trace_id`. For streaming, use the
  [object returned by the stream](async.md#racing-a-stream).
- **The lookup covers the last seven days.** Quality calculations use the recent
  portion of the journal, so older calls are not eligible. `UnknownCallError` is
  raised when no record is found, the call is older than seven days, or no attempt
  completed with a reply.

These restrictions apply only to lookup by `trace_id` or `call_id`. Calling
`record_quality(...)` on a `reply` or stream object performs no journal lookup
and has no seven-day limit.

Record the rating through the same object that made the call. Its scope is taken
from that object rather than from the identifier. For example, rate a call made
through `broker.for_scope(user)` with
`broker.for_scope(user).record_quality(...)`. Calling the method directly on
`broker` records an unscoped rating.

Thresholds and the rating window are configurable — see
[`Optimizer`](reference.md#llmbroker.Optimizer).

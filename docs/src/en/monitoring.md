# Monitoring and the journal

Use `snapshot()` for the pool's current state and the call journal for previous
activity. Both work in a simple script and in a multi-process deployment.

## Pool state {#pool-health}

`snapshot()` returns both pool-wide information and the state of each model:

```python
snap = broker.snapshot()

print(f"{snap.providers_usable} of {snap.providers_total} providers usable")
if snap.degraded:
    print("fewer than two providers are available")

for key in snap.missing_keys:
    print(f"{key.api_key_ref} is required by {', '.join(key.entry_names)}")
    print(key.help)  # where to obtain the key

for key in snap.direct_missing_keys:  # models configured for direct calls
    print(f"{key.api_key_ref} — direct({key.entry_names[0]!r}) will fail")
    print(key.help)

for name, llm in snap.items():  # mapping: name -> model state
    print(name, llm.has_key, llm.cooldown_until)
```

In asynchronous code, use `await broker.snapshot()`. All data are returned in one
object, so no second query is needed for individual model rows. The fields are
documented in [`PoolSnapshot`](reference.md#llmbroker.models.PoolSnapshot).

`direct_missing_keys` is separate from `missing_keys` because models configured
for direct calls are not pool members. A missing key for a direct model does not
affect pool health, and the availability of other models cannot replace it. In
both fields, `help` comes from your `[keys]` section when provided, or from the
maintained catalog otherwise.

Availability is counted by provider (`api_key_ref`), not by model. Models using
the same key share a quota and failure domain, so they count as one provider.
`degraded` is true when fewer than two providers are usable. With one provider,
the pool still works but cannot switch providers after a rate limit. With zero,
it cannot serve requests. `degraded` is false for a registry with no pool models.

A key removed from the secrets store stops counting after the next pool refresh.
The events that trigger a refresh are listed under
[Data shared between processes](server.md#coordination). If a model is
[disabled manually](disable.md), its provider still counts as usable; the model's
own row shows the disabled state.

### Availability alerts {#alerts}

You do not need to poll `snapshot()`. When availability changes, llmbroker writes
a message through the `llmbroker.broker` logger. A monitoring system can alert on
the following messages.

**Only one provider remains usable.** The pool still responds but cannot switch
to another provider after a rate limit. Level `ERROR`:

```
pool degraded, no failover left: 1 of 3 providers usable — no key for GEMINI_API_KEY
```

**No provider is usable.** The pool cannot serve requests. Level `ERROR`:

```
pool cannot serve any request: no provider has a key — no key for GROQ_API_KEY, GEMINI_API_KEY
```

**Every model is temporarily unavailable.** The required keys are configured,
but every model has reached a provider limit. This usually means the registry
contains too few models for the current load. Level `WARNING`, emitted at most
once per minute:

```
pool under-provisioned: all LLMs are COOLING — add more LLMs to the registry
```

The first two messages list any missing keys.

Messages are written only when the state changes, not on every call. A transition
to one provider and a transition to zero providers are separate events, so each
produces its own message.

When at least two providers become available again, one `INFO` message is written:

```
pool recovered: 3 of 3 providers usable
```

The third and later providers do not produce additional recovery messages.

A missing key by itself is not an alert condition if at least two other providers
are usable.

## Call journal {#journal}

Old records are deleted automatically. The journal store's `retention` parameter
controls how long they are kept and defaults to 90 days. See
[Servers & clusters](server.md#journal). Read recent records with
`broker.calls(limit=50)`.

Each attempt creates one row. The fields are documented in
[`Call`](reference.md#llmbroker.models.Call); `score` contains its quality rating
or `None`. Filter rows by time, operation, or call identifier. See
[Finding the entries for one request](#trace).

```python
from datetime import UTC, datetime, timedelta

week_ago = datetime.now(UTC) - timedelta(days=7)
broker.calls(limit=50, since=week_ago, operation="summarize")
```

Filters apply to calls, not to the time a rating was recorded. A later rating is
still displayed on the original call. If a call is rated more than once, the
latest rating is shown.

`since` is inclusive. MongoDB stores time with millisecond precision, so both
stored timestamps and the boundary are rounded down to whole milliseconds.

### Finding the entries for one request {#trace}

`ask`, `chat`, and `stream` accept `trace_id=`, an identifier from your system
such as a request or job ID. llmbroker stores it unchanged on every attempt. Use
it to match journal rows with your application logs.

```python
broker.ask("Summarize this clause", operation="summarize", trace_id=request_id)
```

One call can create several rows because each model attempt is recorded
separately. Every attempt receives the same `trace_id`. The rows can show, for
example, that two models were rate-limited before a third answered. A successful
attempt has status `CallStatus.OK`. Interrupted streaming is not successful even
if some text chunks were already returned.

```python
from llmbroker import CallStatus

rows = broker.calls(limit=200, trace_id=request_id)
answered = next((c for c in rows if c.status is CallStatus.OK), None)
```

Filtering occurs inside the store, so `limit` caps *matching* rows rather than
the number scanned. An old request can still be returned in full when its row
count does not exceed the limit. The field is indexed in database stores. The
file store has no index, so filtering there does not make lookup faster.

To find one attempt, pass `call_id=` with the value from `result.call_id`.

llmbroker allows one `trace_id` to be shared by several calls, but
[rating by identifier](usage.md#quality) applies to only one of them. Use
`call_id` to identify a specific attempt unambiguously. Either identifier can be
used to rate a call after it completes.

### Statistics over a window {#stats}

`stats()` groups records by model and shows the number and final status of
attempts in a time period:

```python
from llmbroker import CallStatus

for name, s in broker.stats(since=week_ago).items():
    failed = s.total - s.by_status.get(CallStatus.OK, 0)
    print(name, s.total, failed, s.last_status, s.last_at)
```

The fields are documented in
[`LLMStats`](reference.md#llmbroker.models.LLMStats).

`by_status` contains only statuses present in the selected period, so calculate
failures from `total` rather than by adding other statuses. `SUPERSEDED` does not
mean the model failed; it identifies a concurrent request that was still running
when another model completed. Exclude it when calculating actual failures.
Recording a rating does not create a call row or affect these counts. Pass
`operation=` to limit the statistics to one operation.

The application decides which statuses count as failures, what period to use, and
how to display models with no calls. llmbroker returns the underlying counts.

`limit`, which defaults to 1000, caps the number of records read rather than the
length of the period. It must be at least 1. If the sum of `total` equals
`limit`, some records may have been omitted. Increase the limit or shorten the
period.

`since` must include a time zone, for example `datetime.now(UTC)` rather than
`datetime.now()`. A value without a time zone is rejected so the boundary does
not depend on the machine's local settings.

`calls()` and `stats()` read only the journal and, unlike `snapshot()`, do not
initialize the pool. A statistics page can therefore work before the registry is
populated. For this use case, construct `Broker(...)` directly without a context
manager. Entering `with Broker(...) as broker` initializes the pool immediately:
an empty registry with automatic downloads disabled raises `EmptyRegistryError`,
while normal settings may download a model list that journal access does not
need.

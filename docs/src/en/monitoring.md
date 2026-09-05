# Monitoring and the journal

Two questions, two answers: what the pool is doing right now — the snapshot; what
it did earlier — the call journal. Both work on any installation, from a script
to a cluster.

## Is the pool healthy {#pool-health}

One call answers that — per-model facts and the pool-wide picture come off the
same object:

```python
snap = broker.snapshot()

print(f"{snap.providers_usable} of {snap.providers_total} providers usable")
if snap.degraded:
    print("nothing to fail over to")

for key in snap.missing_keys:
    print(f"{key.api_key_ref} holds back {', '.join(key.entry_names)}")
    print(key.help)                      # where to get it

for key in snap.direct_missing_keys:     # your own models, reached by name
    print(f"{key.api_key_ref} — direct({key.entry_names[0]!r}) will fail")
    print(key.help)

for name, llm in snap.items():           # still a mapping of name -> per-model facts
    print(name, llm.has_key, llm.cooldown_until)
```

In async code that is `await broker.snapshot()`. The one object also answers a
whole admin screen: the per-model rows and the pool-wide verdict arrive together,
with no second query to fetch them. Fields — in
[`PoolSnapshot`](reference.md#llmbroker.models.PoolSnapshot).

`direct_missing_keys` is separate from `missing_keys` on purpose: a model you
declared is never routed, so a key it lacks cannot degrade the pool and the pool
gaining a provider cannot fix it. Both carry the same `help` — from your own
`[keys]` block if you wrote one, otherwise from the curated catalog.

The unit is the provider (`api_key_ref`), not the model: two entries on one key
are one quota and one failure domain, so they count once. `degraded` is true
while fewer than two providers are usable: at one the pool still answers but a
rate limit has nowhere to spill, at none it no longer answers at all. A registry
that pools nothing has no pool to degrade, so there it is false.

A key revoked in your secrets backend drops out of the count on the next pool
rebuild — there are four of those, all listed in [What processes do and do not
share](server.md#coordination). A model you
[disabled yourself](disable.md) still counts its provider: that verdict is on the
model's own row.

### What to alert on {#alerts}

There is no need to poll `snapshot()` on a schedule: when the pool's health
changes, llmbroker writes a line about it itself — to the `llmbroker.broker`
logger. So an alert hangs off three lines rather than off a metric; if your log
collector can match a substring, that is all it takes.

**The pool has lost its fallback** — one usable provider left. It still answers,
but the first rate limit will have nowhere to spill. Level `ERROR`:

```
pool degraded, no failover left: 1 of 3 providers usable — no key for GEMINI_API_KEY
```

**The pool cannot serve at all** — no usable provider is left. Level `ERROR`:

```
pool cannot serve any request: no provider has a key — no key for GROQ_API_KEY, GEMINI_API_KEY
```

**Providers are there, but every model is cooling right now** — the keys are in
place and the pool is provisioned, there is simply nobody to answer this minute:
they all hit their limits. It means the registry holds too few models for your
traffic. Level `WARNING`, at most once a minute:

```
pool under-provisioned: all LLMs are COOLING — add more LLMs to the registry
```

The first two name the keys that are missing, so they show what to fix.

**A line is written on a change of state, not on every call.** A pool that stays
down would otherwise flood your log; hence one line per transition. And "one
provider left" and "none left" are different events, so the second line comes
after the first rather than instead of it.

Back to normal is one `INFO`:

```
pool recovered: 3 of 3 providers usable
```

It is written once, when two or more providers are usable again; the third and
any after it are not worth a line of their own.

A pending key on its own is never an alarm: two working providers may be exactly
what you provisioned.

## Call journal {#journal}

The journal cleans itself up; the retention depth is the journal backend's
`retention` parameter (90 days by default), see
[Servers & clusters](server.md#journal). To read it: `broker.calls(limit=50)`.

One row per call attempt — fields in [`Call`](reference.md#llmbroker.models.Call),
with `score` holding the quality rating you gave it (or `None`). Narrow the read
by time, by operation, or by one of the ids you called with (see
[Tracing one request](#trace)):

```python
from datetime import UTC, datetime, timedelta

week_ago = datetime.now(UTC) - timedelta(days=7)
broker.calls(limit=50, since=week_ago, operation="summarize")
```

The filters narrow the calls, never the ratings folded onto them: a verdict
recorded a month after the call still shows up on it, and rating a call twice
shows the newer verdict.

`since` is inclusive. On MongoDB it is inclusive to the millisecond — BSON dates
carry no finer precision, so both stored timestamps and the bound are rounded
down to whole milliseconds.

### Tracing one request {#trace}

`ask`, `chat` and `stream` all take `trace_id=` — an id of your own that
llmbroker writes onto every journal row the call produces and never interprets.
Pass whatever your system already uses, a request id or a job id, and the journal
lines up with your logs without a second correlation scheme of its own.

```python
broker.ask("Summarize this clause", operation="summarize", trace_id=request_id)
```

**One call is usually several rows.** Failover journals every attempt it made,
and they all carry the same `trace_id` — which is what the field is for: the
trace keeps the two models that rate-limited before the third one answered, and
that is the evidence for why the request took as long as it did. The attempt that
answered is the row whose `status` is `CallStatus.OK`; a stream that died after
emitting deltas is not it, having never completed.

```python
from llmbroker import CallStatus

rows = broker.calls(limit=200, trace_id=request_id)
answered = next((c for c in rows if c.status is CallStatus.OK), None)
```

The filter runs inside the store, so `limit` caps the *matching* rows rather than
the rows scanned — a trace made an hour and a million calls ago still comes back
whole. On a DB backend the column is indexed; the file store has no index by
construction, so there the filter buys correctness rather than speed.

Pass `call_id=` to pull up a single attempt — `result.call_id` is exactly that
value.

To the journal a `trace_id` is just a field llmbroker stores and filters on, so
nothing stops you grouping several calls under one. But a trace is meant as one
call's id, and [rating by it](usage.md#quality) works on exactly that assumption:
it finds one call, not all of them. Both ids are how you rate a call after the
fact, and `call_id` is the precise one.

### Statistics over a window {#stats}

`stats()` counts call records per model over a time window — how many calls each
model made and how they ended:

```python
from llmbroker import CallStatus

for name, s in broker.stats(since=week_ago).items():
    failed = s.total - s.by_status.get(CallStatus.OK, 0)
    print(name, s.total, failed, s.last_status, s.last_at)
```

Fields — in [`LLMStats`](reference.md#llmbroker.models.LLMStats).

`by_status` holds only the statuses actually seen in the window, so count the
failures by subtracting from `total` rather than by adding up the other statuses.
One status is neither: `SUPERSEDED` is a model that was answering when a faster
sibling answered first — it says nothing about that model, so subtract it too if
you are after a failure count. Rating a call does not add a row, so it cannot
inflate the counts. Pass `operation=` to count one operation only.

What counts as a failure, how long the window should be, and how a model with no
calls in the window should read are yours to decide; llmbroker returns the counts
and no policy.

`limit` (1000 by default) caps how many records are read — a guard against an
anomalous window such as a retry storm, not the window itself. It must be at
least 1. If the totals add up to exactly `limit`, the window may have been
truncated: raise the limit or shorten the window.

`since` must be timezone-aware (`datetime.now(UTC)`, not `datetime.now()`) — a
naive bound is refused rather than guessed at, since guessing would shift the
window by your machine's offset.

`calls()` and `stats()` read the journal only: unlike `snapshot()`, neither
initializes the model pool, so the screen still renders on an installation whose
registry was never synced. Construct the broker directly for that — entering it
as a context manager (`with Broker(...) as broker`) initializes the pool up front:
on an installation that fetches nothing by itself that is `EmptyRegistryError`,
and on an ordinary one a trip for the curated list, which a statistics screen has
no use for.

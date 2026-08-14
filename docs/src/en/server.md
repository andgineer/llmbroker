# Servers & clusters

The same broker scales to multiple processes and hosts: point it at a shared DB
instead of letting it keep its own state — the calling code stays the same.

## Shared DB {#datasource}

The broker's first argument sets the model pool, the keys and the journal all at
once:

```python
llmbroker.Broker()                          # llmbroker's own directory + keys from the environment
llmbroker.Broker("broker.db")               # sqlite
llmbroker.Broker("postgresql://host/db")    # postgres
llmbroker.Broker("mongodb://host/db")       # mongodb
```

Each variant needs its extra — see [Installation](installation.md). Any part can
be overridden explicitly via `registry=` / `secrets=` / `store=`.

### Filling the DB: a deploy job, not a startup step {#sync}

The DB starts empty. Sync it from your own code, in the same deploy step that
runs `alembic upgrade` — built by the factory your application already uses, so
the DSN and its secrets live in exactly one place:

```python
llms = build_broker()                     # your app's own factory
try:
    print(await llms.sync("freetier"))    # the curated preset — the one source there is
finally:
    await llms.aclose()
```

Note this is *not* `async with`: entering the broker provisions the pool, and a
fresh registry is empty, so the context manager would raise `EmptyRegistryError`
before the sync could fill it.

Run it as a one-shot job (release phase, a Kubernetes Job, an init container).
**Keeping the model list current afterwards needs no job at all**: the serving
processes re-check the curated model list themselves, about once a day, on a call they
were making anyway. N nodes checking is safe because they all compute the same
merge from the same upstream and the same keys, so the first write settles it and
every other node's check finds nothing to do. What the design avoids is a node
reconciling the registry against a *local copy* of its own.

`sync` takes a curated preset name and nothing else — no file path, no second
registry. A connection string keeps following the curated preset; hand the broker
a registry *object* and you must say what it follows, `sync="freetier"` or
`sync=None`. Either way a refresh rewrites only the entries a sync itself
wrote — entries your installation states through its own registry are left alone.

### A deployment that may not fetch while it serves {#no-fetch}

An egress policy, an audit rule, a network the serving hosts are not on: where the
process may open no connection except to the providers themselves, switch the
automatic refresh off and do the fetching in the job you already run.

```python
llmbroker.AsyncBroker("postgresql://host/db", sync_interval=None)   # in your factory
```

```python
llms = build_broker()
try:
    report = await llms.sync()        # no argument: whatever this installation follows
    if report is not None:            # the paid catalog alone merges nothing
        print(llmbroker.format_report(report))
finally:
    await llms.aclose()
```

`sync_interval=None` stops every clock in the process that goes online — the
curated model list and the paid catalog your `direct=` aliases resolve through. It
also stops the fetch that fills an empty registry at startup: such a broker raises
`EmptyRegistryError` naming this job, rather than going online to serve its first
request. That is the intended failure. Set the switch and skip the job and the
broker will not serve.

`sync()` with no argument syncs what this installation follows: the preset named by
`sync=`, or — where it follows none — the paid catalog alone, which refreshes your
declared aliases, merges nothing into the registry and therefore returns no report.

A `direct=` alias resolves from whatever your last sync left on the machine, or —
on a host where none has run — from the copy shipped inside the package, and stays
on that version until the job runs again. Nothing here goes online: the alias is
frozen, not broken.

**The freshness is now yours to keep.** Providers retire free endpoints without
notice, so a model list nobody refreshes decays into a pool that cannot serve. Run
the job beside your migrations on every deploy, and on a schedule of its own
between deploys — about once a day is what the built-in clock does, and copying
that is the safe default.

### Moving an installation between backends {#migrate}

There is no migrate command; the two registries already expose everything it
would need, so it is two lines in the same deploy script that holds both DSNs:

```python
old = llmbroker.postgres.Registry(old_pool)
new = llmbroker.mongodb.Registry(new_db)
await new.mirror(await old.load())
```

Secrets and the journal move the same way where you want them to, through their
own backends; usually the keys are re-provisioned and the journal is left behind.

A paid model you reach by name is declared where the factory builds the broker —
`AsyncBroker(dsn, direct=["opus"])` — not written into the registry. One line in
the factory you already have covers the whole cluster, and every process
re-resolves the alias on its own refresh clock, so a long-lived deployment does
not sit on the model id it was first deployed with. See
[Direct model calls](direct.md).

`sync` brings the entries it wrote into line with the curated list it follows: an
entry still on the list is updated, an entry the list no longer carries is
removed, a new one is added. Nothing weighs whether a dropped entry might still
work here, and an entry your own installation put in the registry is never
touched. Removal is bounded where the list is curated instead — an entry leaves
it only once it can no longer be called. The returned `SyncReport` says what
happened, on every run including no-ops, and names any key that has become
unused. A non-zero exit from the job and its log are the admin channel your
failed migrations already use; hosts that forward elsewhere can read
`llms.last_sync_report`.

### Watching the pool from an admin screen {#pool-health}

`snapshot()` is one call and answers the whole screen — the per-model rows plus
the pool-wide verdict:

```python
snap = await llms.snapshot()

health = {
    "providers_usable": snap.providers_usable,
    "providers_total": snap.providers_total,
    "degraded": snap.degraded,
    "missing_keys": [
        {"ref": k.api_key_ref, "help": k.help, "holds_back": list(k.entry_names)}
        for k in snap.missing_keys
    ],
}
rows = [{"name": name, "has_key": llm.has_key} for name, llm in snap.items()]
```

The unit is the provider (`api_key_ref`), not the model: two entries on one key
are one quota and one failure domain, so they count once. `degraded` is true at
one usable provider — the pool answers, but a rate limit has nowhere to spill.

**What to alert on.** llmbroker logs the same verdict, so alerting needs no
polling. Both lines are `ERROR` on the `llmbroker.broker` logger, emitted once
per change of state — the second fires even when the first already has, since
losing your last provider is its own event — and both name the refs that are
missing:

```
pool degraded, no failover left: 1 of 3 providers usable — no key for GEMINI_API_KEY
pool cannot serve any request: no provider has a key — no key for ...
```

Recovery logs one `INFO`; further providers after that are silent. A pending key
on its own is not an alarm; two working providers may be exactly what you
provisioned. A key revoked in your secrets backend drops out of the count on the
next reconcile, so the numbers never lag behind your keys. A model you disabled
yourself still counts its provider — that verdict is already on the per-model
rows, and the alarm is about keys.

## SQLite: sharing and WAL {#sqlite}

The normal setup is one database shared by llmbroker and your application: the
broker keeps its own `llmbroker_*` tables alongside yours and touches nothing
else (the [Alembic](#alembic) hook keeps migration autogenerate clear of them).

That includes `PRAGMA user_version`, the file header slot many migration tools
use — it is yours. The broker keeps its own schema version in an
`llmbroker_schema_version` table, so dropping the `llmbroker_*` tables resets
everything llmbroker holds in the file.

llmbroker never sets or changes SQLite's `journal_mode` — WAL is a persistent,
file-level property that belongs to whoever owns the database file, so enabling
it is your call, not the broker's. On a shared file that owner is your
application: turn on WAL there if you want reader/writer concurrency.

If your application writes to that file heavily, give the broker its own file
instead — point it at a separate `.db` and the two stop contending on SQLite's
file-level lock. A file that is the broker's alone is yours to configure
directly; WAL is set once and persists:

```bash
sqlite3 broker.db 'PRAGMA journal_mode=WAL'
```

This applies to SQLite only. Postgres and MongoDB have no equivalent file-level
lock — sharing one database with your application is fine, and a dedicated
schema or database is optional tidiness, not a concurrency need.

## Startup errors {#errors}

Two conditions can stop the broker before it serves a single request, and a host
usually wants to treat them differently:

- `EmptyRegistryError` — nothing has been synced into the registry yet. Benign:
  the installation is unconfigured, not broken.
- `SyncRefusedError` — raised by `sync()` when applying its result would leave a
  working registry with no entries at all. Nothing was written; `report` carries
  what the merge would have done.
- `SchemaVersionError` — the store holds a schema version this release cannot
  use. Fatal and operator-actionable: drop the `llmbroker_*` tables and restart
  (export registry/secrets/calls first if you need them). `found` and `expected`
  carry the two versions.

Both subclass `LLMBrokerError`, itself a `RuntimeError`, so catch at the
granularity you need:

```python
try:
    models = llms.snapshot()
except llmbroker.EmptyRegistryError:
    models = {}   # nothing configured yet — render an empty screen, not a 500
```

`SchemaVersionError` propagates: its message is the operator's instruction, so
swallowing it turns a schema mismatch into "no providers configured". Catching
`LLMBrokerError` covers both, and `RuntimeError` covers both plus everything
else.

A failed request raises from a separate tree (`LLMRequestError` and its
subclasses) — see [Calling the broker](usage.md#calling).

## Closing the broker {#closing}

Close the broker explicitly when a long-lived process creates brokers repeatedly
or an external DB is attached:

```python
with llmbroker.Broker("broker.db") as llms:
    reply = llms.ask("...")
```

`AsyncBroker` — `async with` or `await llms.aclose()`.

## Call journal

The journal cleans itself up; the retention depth is the journal backend's
`retention` parameter (90 days by default). To read it: `llms.calls(limit=50)`.

The journal holds two kinds of record — calls and quality ratings — interleaved
in one stream. Narrow the read by kind, by time, by operation, or by one of the
ids you called with (see [Tracing one request](#trace)):

```python
from datetime import UTC, datetime, timedelta

week_ago = datetime.now(UTC) - timedelta(days=7)
llms.calls(limit=50, kind="call", since=week_ago, operation="summarize")
```

`since` is inclusive. On MongoDB it is inclusive to the millisecond — BSON dates
carry no finer precision, so both stored timestamps and the bound are rounded
down to whole milliseconds.

### Tracing one request {#trace}

`ask`, `chat` and `stream` all take `trace_id=` — an id of your own that
llmbroker writes onto every journal row the call produces and never interprets.
Pass whatever your system already uses, a request id or a job id, and the journal
lines up with your logs without a second correlation scheme of its own.

```python
llms.ask("Summarize this clause", operation="summarize", trace_id=request_id)
```

**One call is usually several rows.** Failover journals every attempt it made,
and they all carry the same `trace_id` — which is what the field is for: the
trace keeps the two models that rate-limited before the third one answered, and
that is the evidence for why the request took as long as it did. The attempt that
answered is the row whose `status` is `CallStatus.OK`; a stream that died after
emitting deltas is not it, having never completed.

```python
from llmbroker.models import CallStatus

rows = llms.calls(limit=200, kind="call", trace_id=request_id)
answered = next((c for c in rows if c.status is CallStatus.OK), None)
```

The filter runs inside the store, so `limit` caps the *matching* rows rather than
the rows scanned — a trace made an hour and a million calls ago still comes back
whole. On a DB backend the column is indexed; the file store has no index by
construction, so there the filter buys correctness rather than speed.

Pass `call_id=` to pull up a single attempt — `result.call_id` is exactly that
value. Mind the name: it matches a call row's own `id`, not the same-named column
a quality rating carries pointing back at the call it rates.

Reusing one `trace_id` across several calls is fine and groups them: llmbroker
only ever stores it.

### Statistics over a window

`stats()` counts call records per model over a time window — how many calls each
model made and how they ended:

```python
from llmbroker.models import CallStatus

for name, s in llms.stats(since=week_ago).items():
    failed = s.total - s.by_status.get(CallStatus.OK, 0)
    print(name, s.total, failed, s.last_status, s.last_at)
```

Fields — in [`LLMStats`](reference.md#llmbroker.models.LLMStats).

`by_status` holds only the statuses actually seen in the window, so count the
failures by subtracting from `total` rather than by adding up the other statuses.
Quality ratings are never counted — they are not calls. Pass `operation=` to
count one operation only.

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
initializes the model pool, so an admin screen still renders on an installation
whose registry was never synced. Construct the broker directly for that —
entering it as a context manager (`with Broker(...) as llms`) initializes the
pool up front and will raise `EmptyRegistryError` on such an installation.

## One broker, a caller per request {#multiuser}

**A broker is the installation.** It holds the model pool, the keys, everything
it has learned and one HTTP client, and it lives as long as the process. What a
request holds is a *caller*: the scope its journal rows are attributed to and the
keys it may pay with, over that one shared pool. Asking for a caller costs no I/O,
so building one per request is the intended shape.

Four deployments, in order of how much they need:

**A script.** Nothing to hold, nothing to share — the broker's own call verbs are
its unscoped caller, so the second noun never appears:

```python
llms = llmbroker.Broker()
print(llms.ask("hi").text)
```

**A long-lived process with a database.** Build the broker where you build your
database engine — once, at startup — and close it at shutdown:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.broker = llmbroker.AsyncBroker("postgresql://host/db")
    try:
        yield
    finally:
        await app.state.broker.aclose()
```

**A cluster on shared keys.** Every handler takes the broker's own caller. One
pool, one set of keys, one connection pool per process:

```python
def llms(request: Request) -> llmbroker.AsyncLLMs:
    return request.app.state.broker.llms

@app.post("/ask")
async def ask(prompt: str, llms: llmbroker.AsyncLLMs = Depends(llms)):
    return (await llms.ask(prompt)).text
```

**The same cluster with a key per user.** Only the dependency changes:

```python
def llms(request: Request) -> llmbroker.AsyncLLMs:
    return request.app.state.broker.for_scope(request.headers["x-user-id"])
```

A scoped caller resolves its own key first — the ref prefixed with its scope —
and falls back to the installation's shared one. The shared value is read once
for everybody. Every row that caller journals carries its scope, so a store-level
`calls(scope=...)` gives you one user's history; the broker's own `calls()` and
`stats()` are the installation's view and are not scoped.

The pool, the quality it has learned and the per-model `parallel` cap belong to
the broker, not the caller — one counter per user would not be a cap at all.
A key one caller's provider rejects stops being offered to that caller; a caller
holding a key of its own is untouched, and callers sharing one value lose it
together, because that is one credential.

### What processes do and do not share {#coordination}

**Processes do not coordinate.** Each keeps its own view of which models are
currently available: a cooldown one process met, and a key one process found
dead, are that process's findings and are never read by another. The cost is one
wasted call per process, absorbed by failover and invisible to the caller.

**A peer's registry edit arrives at the next rebuild.** The pool is rebuilt at
start, on the refresh clock (about once a day), on an explicit `sync()`, and when
the pool has just failed to answer. Nothing else re-reads the ports, so a
successful call costs no database traffic beyond its own journal row.

**A key stored into a running installation is picked up by the first call that
needs it.** That last trigger is what makes it work: a call the pool cannot serve
re-reads the keys and then answers from them, so the caller sees no error at all —
no restart, and no waiting out the clock. The re-read is skipped for a caller that
already holds every key, since no key that appeared could help it, and it happens
at most once a minute. A rebuild that finds the same rejected value keeps the
rejection, so a key that is simply dead costs one call per period rather than one
per request.

## Alembic

To make migration autogeneration ignore the `llmbroker_*` tables:

```python
# alembic/env.py
import llmbroker.integrations.alembic

context.configure(
    connection=connection,
    target_metadata=target_metadata,
    include_object=llmbroker.integrations.alembic.include_object,
)
```

Combine your own `include_object` with it via `and`.

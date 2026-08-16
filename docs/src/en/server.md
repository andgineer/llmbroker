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

### A registry of your own {#own-registry}

To supply the pool yourself, pass an object implementing the registry protocol
and say what it follows:

```python
broker = llmbroker.Broker(registry=MyRegistry(), sync=None)        # only your entries
broker = llmbroker.Broker(registry=MyRegistry(), sync="freetier")  # yours plus ours
```

| you pass | `sync=` left out | entries you put there yourself |
|---|---|---|
| nothing, or a database URL | follows `"freetier"` | never touched by a refresh |
| a registry object | an error — say which | never touched by a refresh |

**A registry of your own covers the registry only.** Keys and the journal are
separate ports, and bringing your own registry does not make them yours: the keys
will still be read from the environment, and the journal will land in a `store`
directory beside the process's working directory. For a service that is almost
certainly not what you wanted — pass both explicitly:

```python
from llmbroker.postgres import Secrets, Store

broker = llmbroker.AsyncBroker(
    registry=MyRegistry(),
    secrets=Secrets(pool),            # keys in your database, not in the environment
    store=Store(pool),                # the journal there too
    sync=None,
)
```

A refresh only ever rewrites what a sync itself wrote, so "the curated free pool
plus two endpoints of my own, routed together" is just a registry with both in
it.

#### An entry of your own, in the pool {#own-entry}

You put your own entry there through the registry protocol, not by writing rows:
the table layout is llmbroker's own and may change between releases. Read what is
there, add yours, write it all back — `mirror` is a total mirror, so anything you
leave out is deleted:

```python
from llmbroker import LLMConfig
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

### Filling the DB: a deploy job, not a startup step {#sync}

The DB starts empty. Sync it from your own code, in the same deploy step that
runs `alembic upgrade` — built by the factory your application already uses, so
the DSN and its secrets live in exactly one place:

```python
broker = build_broker()                   # your app's own factory
try:
    print(await broker.sync("freetier"))  # the curated preset — the one source there is
finally:
    await broker.aclose()
```

Note this is *not* `async with`: entering the broker provisions the pool, and the
deploy job has exactly one thing to do — fill the database. And where the broker
may not reach the network ([below](#no-fetch)), the context manager raises
`EmptyRegistryError` right there, before you ever call `sync()`.

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
broker = build_broker()
try:
    report = await broker.sync()      # no argument: whatever this installation follows
    if report is not None:            # the paid catalog alone merges nothing
        print(llmbroker.format_report(report))
finally:
    await broker.aclose()
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
from llmbroker.mongodb import Registry as MongoRegistry
from llmbroker.postgres import Registry as PostgresRegistry

old = PostgresRegistry(old_pool)
new = MongoRegistry(new_db)
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
`broker.last_sync_report`.

### Watching the pool from an admin screen {#pool-health}

`snapshot()` is one call and answers the whole screen — the per-model rows plus
the pool-wide verdict — and llmbroker logs that same verdict, so alerting needs
no polling. See [Monitoring and the journal](monitoring.md#pool-health).

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

Three conditions can stop the broker before it serves a single request, and a host
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

All three live on `llmbroker` (`llmbroker.SchemaVersionError`, and so on) and
subclass `LLMBrokerError`, itself a `RuntimeError`, so catch at the granularity
you need:

```python
try:
    models = broker.snapshot()
except llmbroker.EmptyRegistryError:
    models = {}   # nothing configured yet — render an empty screen, not a 500
```

`SchemaVersionError` propagates: its message is the operator's instruction, so
swallowing it turns a schema mismatch into "no providers configured". Catching
`LLMBrokerError` covers all three, and `RuntimeError` covers them plus everything
else.

A failed request raises from a separate tree (`LLMRequestError` and its
subclasses) — see [When nobody can answer](usage.md#errors).

## Closing the broker {#closing}

Close the broker explicitly when a long-lived process creates brokers repeatedly
or an external DB is attached:

```python
with llmbroker.Broker("broker.db") as broker:
    reply = broker.ask("...")
```

`AsyncBroker` — `async with` or `await broker.aclose()`.

## Call journal {#journal}

Every call attempt leaves a row: what answered, how it ended, what it cost, the
`trace_id` you called it with, and how you rated it afterwards. It is read with
`broker.calls(...)` and `broker.stats(...)`, neither of which initializes the
pool, so both work on an installation that was never synced. See
[Monitoring and the journal](monitoring.md#journal).

The journal cleans itself up: rows older than `retention` are dropped, 90 days by
default. The depth belongs to the journal backend rather than to the broker, so a
connection string cannot set it — assemble the ports yourself and set it on the
one that writes the journal.

```python
from datetime import timedelta

from llmbroker.postgres import Registry, Secrets, Store

broker = llmbroker.AsyncBroker(
    registry=Registry(pool),
    secrets=Secrets(pool),
    store=Store(pool, retention=timedelta(days=365)),
    sync="freetier",                  # a registry object — say what it follows
)
```

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
broker = llmbroker.Broker()
print(broker.ask("hi").text)
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

**A user's key lives under a ref with its scope in front: `<scope>/<REF>`.** A
caller scoped `u-42` asks the secrets store for `u-42/GROQ_API_KEY` first and only
then falls back to the installation's shared `GROQ_API_KEY`; the shared value is
read once for everybody. So giving a user a key of their own means storing it
under that name — in an environment variable, in your database, in AWS or Vault,
wherever the shared ones live. The scope is simply the string you passed to
`for_scope(...)`; llmbroker has no notion of a user inside it. Vault has one
caveat about the `/` in that name — see [API keys](secrets.md#vault).

Every row that caller journals carries its scope, so one user's history is
`broker.for_scope(user).calls(...)`. There is no `scope=` parameter on `calls()`:
the scope comes from the caller you read through. The broker's own
`broker.calls()` and `broker.stats()` are the installation's view and see every
scope's rows at once.

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

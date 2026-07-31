# Servers & clusters

The same broker scales to multiple processes and hosts: switch it from a file to
a shared DB — the calling code stays the same.

## Shared DB {#datasource}

The broker's first argument sets the model pool, the keys and the journal all at
once:

```python
llmbroker.Broker("llms.toml")               # files + keys from the environment
llmbroker.Broker("broker.db")               # sqlite
llmbroker.Broker("postgresql://host/db")    # postgres
llmbroker.Broker("mongodb://host/db")       # mongodb
```

Each variant needs its extra — see [Installation](installation.md). Any part can
be overridden explicitly via `registry=` / `secrets=` / `store=`.

The DB starts empty — load a preset into it once, e.g. on deploy:

```bash
llmbroker sync llms.toml "postgresql://host/db"
```

A repeated `sync` is a full synchronization with the file: it adds, updates and
deletes entries; deletion loses no accumulated model history. The same from
code: `await llms.sync(llmbroker.Registry("llms.toml"))`.

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
in one stream. Narrow the read by kind, by time, or by operation:

```python
from datetime import UTC, datetime, timedelta

week_ago = datetime.now(UTC) - timedelta(days=7)
llms.calls(limit=50, kind="call", since=week_ago, operation="summarize")
```

`since` is inclusive. On MongoDB it is inclusive to the millisecond — BSON dates
carry no finer precision, so both stored timestamps and the bound are rounded
down to whole milliseconds.

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

## A key per user {#multiuser}

`scope=` gives every user their own API key on top of one shared pool:

```python
async with llmbroker.AsyncBroker("broker.db", scope=user_id) as llms:
    reply = await llms.ask(prompt)
```

The key is looked up by the user's scope first, then the shared one. The model
pool and everything it learns are shared by all; the journal carries `scope` —
filter `calls(...)` by it.

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

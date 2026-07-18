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

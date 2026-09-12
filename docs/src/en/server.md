# Servers & clusters

For multiple processes or hosts, configure the broker with a shared database
instead of local storage. The call API remains unchanged.

## Shared database {#datasource}

The broker's first argument selects storage for the model registry, keys, and
journal:

```python
llmbroker.Broker()  # local directory and environment keys
llmbroker.Broker("broker.db")  # SQLite
llmbroker.Broker("postgresql://host/db")  # PostgreSQL
llmbroker.Broker("mongodb://host/db")  # MongoDB
```

Install the matching optional dependency for each database. See
[Installation](installation.md). Configure individual stores with `registry=`,
`secrets=`, and `store=`.

### A custom model registry {#own-registry}

To manage the pool yourself, pass an object that implements the registry protocol
and explicitly choose its update mode:

```python
broker = llmbroker.Broker(registry=MyRegistry(), sync=None)  # only your entries
broker = llmbroker.Broker(registry=MyRegistry(), sync="freetier")  # yours plus ours
```

| registry source | if `sync=` is omitted | entries you add yourself |
|---|---|---|
| nothing or a database URL | uses `"freetier"` | unchanged by updates |
| a registry object | error: choose a mode | unchanged by updates |

`registry=` replaces only the registry. Key and journal storage are configured
separately. If omitted, keys are read from environment variables and the journal
is written to a local `store` directory. Server applications should normally
configure all three stores explicitly:

```python
from llmbroker.postgres import Secrets, Store

broker = llmbroker.AsyncBroker(
    registry=MyRegistry(),
    secrets=Secrets(pool),  # keys in the database
    store=Store(pool),  # journal in the same database
    sync=None,
)
```

An update modifies only entries created by previous syncs. A single registry can
therefore contain both the maintained free list and your own models.

#### Adding your own model to the pool {#own-entry}

Modify the registry through its API rather than writing database rows directly;
the internal schema can change between llmbroker releases. Load the existing
entries, add yours, and pass the complete list to `mirror()`. This method replaces
the entire registry, so entries omitted from the call are deleted:

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

This is a user-managed entry, so synchronization never removes or changes it. The
model participates in normal pool selection and can be used after another model
fails. To call a model only by name, configure it as a [direct
model](direct.md); direct configurations are not stored in the registry.

### Populating the database during deployment {#sync}

A new database contains no models. Populate it in a deployment job, for example
next to `alembic upgrade`. Use the same broker-construction function as the
application so the connection string and related secrets are configured in one
place:

```python
broker = build_broker()  # the application's broker factory
try:
    print(await broker.sync("freetier"))  # maintained model-list name
finally:
    await broker.aclose()
```

Do not use `async with` here. Entering the context manager initializes the pool,
but this job must populate an empty database first. If automatic network access
is disabled, `EmptyRegistryError` is raised before `sync()` can run. See
[Deployment without automatic network access](#no-fetch).

Run this code as a one-time release task, Kubernetes Job, or init container. No
separate update job is required afterwards: serving processes check the
maintained list about once a day during normal calls. Multiple processes can
check safely because they use the same source and keys. The first writes any
changes; the others find nothing left to update. A process's local cached copy is
not used to overwrite the shared registry.

`sync` accepts only a maintained list name, not a file path or second registry.
With a connection string, `freetier` is selected by default. When `registry=` is
an object, explicitly set `sync="freetier"` or `sync=None`. In either case, an
update modifies only entries created by earlier syncs and preserves user-managed
entries.

### Deployment without automatic network access {#no-fetch}

If serving processes may contact only model-provider APIs, disable automatic
catalog updates and run them from a separate deployment job. Network policy or
audit requirements may require this configuration.

```python
llmbroker.AsyncBroker("postgresql://host/db", sync_interval=None)  # in broker construction
```

```python
broker = build_broker()
try:
    report = await broker.sync()  # no argument: whatever this installation follows
    if report is not None:  # paid-catalog-only updates have no report
        print(llmbroker.format_report(report))
finally:
    await broker.aclose()
```

`sync_interval=None` disables automatic network calls to update both the free
model list and the paid catalog used by `direct=` aliases. It also disables
automatic population of an empty registry. In that case, the broker raises
`EmptyRegistryError` referring to the sync job instead of accessing the network
for the first user request.

`sync()` without an argument updates the list selected by `sync=`. If
`sync=None`, it updates only the paid catalog required by aliases in `direct=`.
Nothing is written to the registry in that case, so the method returns no report.

A `direct=` alias is resolved from data written by the latest sync. If sync has
never run, the broker uses the catalog copy bundled with the installed package.
The selected model version remains unchanged until the job runs again; no
automatic network request is made.

When automatic updates are disabled, you must keep the list current. Free-model
availability can change without notice. Run the job during every deployment and,
if needed, on a schedule between releases. The standard automatic interval is
about one day and is a reasonable default.

### Moving data between storage backends {#migrate}

There is no separate migration command. Load entries from one registry and write
them to another in a deployment task that has both connection strings:

```python
from llmbroker.mongodb import Registry as MongoRegistry
from llmbroker.postgres import Registry as PostgresRegistry

old = PostgresRegistry(old_pool)
new = MongoRegistry(new_db)
await new.mirror(await old.load())
```

When needed, move keys and journal records through their respective storage APIs.
In practice, keys are often issued again and the old journal is retained in its
original location.

A paid model for direct calls is configured when the broker is created:
`AsyncBroker(dsn, direct=["opus"])`. It is not written to the registry. One
setting in the shared broker-construction function applies to every process.
Each process periodically checks the alias against the current model version, so
long-lived deployments also receive updates. See [Direct model calls](direct.md).

`sync` makes its own entries match the selected maintained list: existing entries
are updated, missing entries are removed, and new entries are added.
User-managed registry entries are unchanged. A model is removed from the
maintained list only after it can no longer be called.

`SyncReport` describes all changes and names keys that are no longer used. A
report is returned even when nothing changed. Handle task failures like database
migration failures. The latest report is also available through
`broker.last_sync_report` for forwarding to another system.

### Displaying pool state {#pool-health}

`snapshot()` returns each model's state and pool-wide information in one call.
llmbroker also logs availability changes, so monitoring does not need to poll the
application. See [Monitoring and the journal](monitoring.md#pool-health).

## SQLite: sharing and WAL {#sqlite}

llmbroker and the application can share one SQLite file. The broker creates only
`llmbroker_*` tables and does not modify other tables. The [Alembic](#alembic)
integration excludes these tables from migration autogeneration.

llmbroker does not change `PRAGMA user_version`, which many migration tools use.
Its own schema version is stored in `llmbroker_schema_version`. Dropping all
`llmbroker_*` tables removes all llmbroker data from the file.

llmbroker does not change SQLite's `journal_mode`. WAL mode persists in the file
and should be configured by the application that manages it. Enable WAL when
multiple connections need concurrent reads and writes.

If the application writes frequently, give the broker a separate `.db` file to
reduce contention on SQLite's file lock. WAL mode needs to be enabled only once
for a separate file:

```bash
sqlite3 broker.db 'PRAGMA journal_mode=WAL'
```

This limitation applies only to SQLite. PostgreSQL and MongoDB do not use an
equivalent shared file lock, so they can share a database with the application.
A separate schema or database is an organizational choice.

## Upgrading llmbroker {#upgrade}

llmbroker never migrates an existing database in place. When a release changes
the internal schema, the broker refuses to start and raises `SchemaVersionError`
instead of reading the old tables. The upgrade is a reset: drop the
`llmbroker_*` tables and let the new release create them again.

Save what you need before dropping. The registry is restored by the next
`sync()` and the journal is history, but keys held in the broker's own store
exist nowhere else, so exporting them is a required step of the upgrade rather
than an optional one. This applies only to keys in the database; keys that come
from the environment, AWS Secrets Manager, or Vault are untouched by the reset.

**Export the keys before installing the new release.** The new release refuses
to read the old tables, so its API can no longer reach them — the export must
run while the old version is still installed:

```python
import json
from pathlib import Path

from llmbroker.sqlite import Secrets  # or llmbroker.postgres / llmbroker.mongodb

secrets = Secrets("broker.db")
dump = {ref: await secrets.resolve(ref) for ref in sorted(await secrets.refs())}
Path("keys.json").write_text(json.dumps(dump))
```

The file holds the keys in plain text: keep it outside the repository and delete
it once the upgrade is finished. If the old version is already gone, read the
rows with the database's own client instead:

```bash
sqlite3 broker.db 'SELECT ref, value FROM llmbroker_secrets'
```

Then drop the tables, start the new release, and restore the data:

```python
for ref, value in json.loads(Path("keys.json").read_text()).items():
    await secrets.set(ref, value)
await broker.sync("freetier")
```

The pool starts cold after a reset: earlier calls, quality history, and active
cooldowns are gone, and the models are ranked again from the first calls of the
new deployment.

## Startup errors {#errors}

Three errors can occur before the first request and require different handling:

- `EmptyRegistryError` — the registry has not been populated. Run the initial
  setup or synchronization.
- `SyncRefusedError` — `sync()` did not apply an update because it would leave
  the registry empty. The registry is unchanged, and `report` contains the
  planned changes.
- `SchemaVersionError` — the stored schema version is incompatible with this
  llmbroker release. `found` and `expected` contain the detected and required
  versions. See [Upgrading llmbroker](#upgrade).

All three classes are available directly from `llmbroker`, for example
`llmbroker.SchemaVersionError`. They inherit `LLMBrokerError`, which inherits
`RuntimeError`. Catch either a specific error or the common base class:

```python
try:
    models = broker.snapshot()
except llmbroker.EmptyRegistryError:
    models = {}  # display an empty state instead of HTTP 500
```

Do not hide `SchemaVersionError`; its message contains instructions for the
operator. Otherwise an incompatible schema can look like a missing provider
configuration. `LLMBrokerError` catches all three errors above, while
`RuntimeError` also catches unrelated runtime errors.

Request failures inherit a separate class, `LLMRequestError`. See
[When nobody can answer](usage.md#errors).

## Closing the broker {#closing}

Close the broker explicitly when a process creates brokers repeatedly or uses an
external database:

```python
with llmbroker.Broker("broker.db") as broker:
    reply = broker.ask("...")
```

For `AsyncBroker`, use `async with` or `await broker.aclose()`.

## Call journal {#journal}

Each model attempt records the model name, outcome, usage information, `trace_id`,
and later rating. `broker.calls(...)` and `broker.stats(...)` read the journal
without initializing the pool, so they work before the registry is populated.
See [Monitoring and the journal](monitoring.md#journal).

Rows older than `retention` are deleted automatically; the default is 90 days.
This setting belongs to the journal store and cannot be set in the broker's
connection string. Construct the stores explicitly and pass `retention` to
`Store`:

```python
from datetime import timedelta

from llmbroker.postgres import Registry, Secrets, Store

broker = llmbroker.AsyncBroker(
    registry=Registry(pool),
    secrets=Secrets(pool),
    store=Store(pool, retention=timedelta(days=365)),
    sync="freetier",  # explicitly select the maintained list
)
```

## One broker for multiple users {#multiuser}

Create one broker for the lifetime of each process. It owns the model pool, keys,
quality data, and HTTP client. For an individual user, `for_scope(...)` creates a
lightweight `AsyncLLMs` object. It uses the shared pool while associating journal
rows and user-specific keys with the supplied scope. Creating this object performs
no storage I/O, so it can be done for every incoming request.

The following examples cover four common configurations.

**A simple script.** Call methods on the broker itself; no separate scope is
needed:

```python
broker = llmbroker.Broker()
print(broker.ask("hi").text)
```

**A long-lived process with a database.** Create the broker once at application
startup and close it during shutdown:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.broker = llmbroker.AsyncBroker("postgresql://host/db")
    try:
        yield
    finally:
        await app.state.broker.aclose()
```

**Multiple processes with shared keys.** Each handler receives the shared
`AsyncLLMs` object. Requests use one model pool and one key set, with one
connection pool per process:

```python
def llms(request: Request) -> llmbroker.AsyncLLMs:
    return request.app.state.broker.llms


@app.post("/ask")
async def ask(prompt: str, llms: llmbroker.AsyncLLMs = Depends(llms)):
    return (await llms.ask(prompt)).text
```

**A separate key for each user.** Only the dependency function changes:

```python
def llms(request: Request) -> llmbroker.AsyncLLMs:
    return request.app.state.broker.for_scope(request.headers["x-user-id"])
```

A user-specific key is stored as `<scope>/<key name>`. For example, an object for
scope `u-42` first requests `u-42/GROQ_API_KEY` and uses the shared
`GROQ_API_KEY` if the scoped key is absent. The shared key is read once and reused
across scopes. Store scoped keys in the same source as shared keys: environment
variables, a database, AWS Secrets Manager, or Vault.

A scope is simply a string passed to `for_scope(...)`. llmbroker assigns it no
special meaning and does not require it to represent a user. See
[API keys](secrets.md#vault) for the handling of `/` in Vault names.

Each journal row stores the scope of the object that made the call. Read one
user's history with `broker.for_scope(user).calls(...)`. `calls()` has no separate
`scope=` parameter; the scope comes from the object. `broker.calls()` and
`broker.stats()` return rows from every scope.

The model pool, quality data, and each model's `parallel` limit belong to the
broker rather than an individual scope. Separate concurrency counters per user
would violate the provider's limit. If a provider rejects a user-specific key,
that key stops being used only in the corresponding scope. If several scopes use
the same key value, the rejection applies to all of them.

### Data shared between processes {#coordination}

Temporary model availability is not shared between processes. Each process tracks
provider limits and rejected keys independently. The same failed request may
therefore occur once in each process before the broker moves to another model.
This transition is automatic for the user.

Changes to the shared registry become visible to a process at the next pool
refresh: at startup, about once a day, after an explicit `sync()`, or after no
model could answer a request. The registry and keys are not reread in other
cases, so a successful call writes only its own journal row to the database.

A newly stored key becomes available without restarting the process. If no model
can serve a request, the broker rereads missing keys and immediately repeats model
selection. The application does not receive the intermediate failure. The check
is skipped when all keys were already available and runs at most once per minute.
If the store returns the same rejected value, the broker retains the rejection
and does not retry that key on every request.

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

If the application already defines `include_object`, combine it with this check
using `and`.

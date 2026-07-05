# Usage

## Configuration

Create `llms.toml` with a list of LLMs:

```toml
[[llms]]
name        = "groq-llama"
base_url    = "https://api.groq.com/openai/v1"
model       = "llama-3.3-70b-versatile"
api_key_ref = "GROQ_API_KEY"

[[llms]]
name        = "groq-gemma"
base_url    = "https://api.groq.com/openai/v1"
model       = "gemma2-9b-it"
api_key_ref = "GROQ_API_KEY"
```

A ready-made list of free LLMs is available as a preset:

```bash
llmbroker preset freetier > llms.toml
```

Free-tier offerings drift, so this preset is refreshed periodically from current
sources — see `presets/freetier-refresh-prompt.md` (run `invoke catalog-refresh`
to print it) and [`freetier-providers.md`](https://github.com/andgineer/llmbroker/blob/main/specs/reference/freetier-providers.md),
the source-of-record it reads and updates.

`api_key_ref` is the name of the environment variable holding the key. To print the
required variable names for a config file:

```bash
llmbroker env llms.toml
```

These are only the variable names — get the actual keys from each provider and set them.
A `.env` file is the simplest path, but API key can come from any Secrets backend (environment, DB,
AWS, Vault, …).

### Where to get the keys

When a config carries a `[keys]` table — as the bundled presets do — `llmbroker env`
prints a comment above each variable telling you where to obtain that key:

```
# GROQ_API_KEY — Create a free API key at [groq](https://console.groq.com/keys) (sign in, then New API Key).
GROQ_API_KEY=
```

To show the same hints in your own UI, read them programmatically from a file registry:

```python
import asyncio
import llmbroker

registry = llmbroker.Registry("llms.toml")
info = asyncio.run(registry.key_info())
# {"GROQ_API_KEY": KeyInfo(api_key_ref="GROQ_API_KEY", help="Create a free API key at [groq](...) ...",
#                          extra={"effort": "signup", "value": "good"}), ...}
```

`key_info()` returns a `KeyInfo` (markdown `help`, plus a free-form `extra: dict[str, str]`
passthrough of whatever else the TOML `[keys.REF]` section holds — llmbroker has no
taxonomy opinion on it) per `api_key_ref`. It is an optional registry capability
(`KeyInfoProtocol` in `llmbroker.protocols.registry`): registries that carry the
metadata expose it, others do not — probe with `isinstance(registry, KeyInfoProtocol)`
if you accept arbitrary registries. It is independent of the broker, so you do not need
to wire the registry into a broker to read it. Missing a key for some models is the
normal way to run llmbroker — the pool routes over whatever keys are present; only zero
usable models is an error.

## Calling the broker

`AsyncBroker` is the core engine — use it in FastAPI, agents, and async workers:

```python
import llmbroker

async def main():
    async with llmbroker.AsyncBroker("llms.toml") as llms:
        # Single prompt
        reply = await llms.ask("Translate to French: Hello world")
        print(reply.text)

        # Full messages API
        reply = await llms.chat([
            {"role": "system", "content": "You are a concise assistant."},
            {"role": "user",   "content": "What is Python?"},
        ])
        print(reply.text)
```

### Sync wrapper

`Broker` wraps the same engine behind a blocking API — for scripts and synchronous
apps. It runs an internal event loop on a background thread; the methods are identical,
just without `await`:

```python
import llmbroker

llms = llmbroker.Broker("llms.toml")
print(llms.ask("Translate to French: Hello world").text)
```

## Controlling requests

### Wait timeout

By default a call waits until any LLM becomes free. To limit the wait:

```python
from llmbroker import NoLLMAvailableError

try:
    reply = llms.ask("Question", wait=5.0)   # at most 5 seconds
except NoLLMAvailableError:
    print("All LLMs are busy")
```

`wait=0` — fail immediately if no slot is free.

### Quality feedback

```python
reply = llms.ask("Classify as positive or negative: 'Fast delivery and great packaging!'")
# ... inspect the result ...
reply.record_quality(1.0)   # good answer
reply.record_quality(0.0)   # bad answer
```

The score lands in the journal as its own self-contained record (never joined back onto
the call it rates) and, when the optimizer is active (the default), feeds that model's
per-operation quality window — see "Learning & selection" below.

### Operations

Tag a call with the kind of task it is doing via `operation=`. Quality feedback and
demotion are keyed by `(llm, operation)`, because a model's usefulness is genuinely
task-shaped: fine on simple tasks, weak on hard ones. Calls made without `operation=`
fall into one shared bucket (keyed by `None`).

```python
reply = await llms.ask("Summarize this contract clause", operation="summarize")
reply.record_quality(0.9)  # rated against the "summarize" bucket specifically
```

## Learning & selection

When the optimizer is active (`optimize=True`, the default), every rated call is
folded into a per-`(model, operation)` sliding window of the last `quality_window`
ratings (default 30). A bucket is **demoted** once it holds at least
`quality_min_count` ratings (default 10) and their Wilson-score upper bound sits below
`quality_floor` (default 0.3).

Selection is a single sort: within the requested operation, a demoted model sorts
after every non-demoted one; among slots with the same demotion verdict, curated
order wins (each model's position in the registry/preset — lower is better). Demotion
is always soft — a demoted-only pool still serves the request, since the quality
signal is your own opinion and may be miscalibrated. There is no global "bad model"
verdict: the same model can be demoted for `"classify"` and fine for `"summarize"`.

Recovery is exactly new ratings displacing the window: once the bound climbs back
above the floor, the model is no longer demoted. There is no time-based recovery, no
probation traffic, and no explicit "reset quality" call — the only way to reset a
model's learned quality is to keep sending it ratings.

This learned state (score windows, shared 429/503 cooldowns, quality-demotion flips)
is derived from the call journal, re-read on a debounce — see "Production" below for
where it persists. See
[`optimizer.md`](https://github.com/andgineer/llmbroker/blob/main/specs/reference/optimizer.md)
for the full mechanics.

## Administration

`disable_llm` is a manual, hard verdict — separate from (and stronger than) quality
demotion:

```python
await llms.disable_llm("groq-llama")
# ... later ...
await llms.enable_llm("groq-llama")
```

`disable_llm` withdraws the model from routing entirely — every operation, including
future ones — surviving preset syncs, until `enable_llm` clears it. It is stored in the
store's hand-editable `store/disabled.yml` (or the equivalent DB table), so an admin can
flip it directly without going through the broker. `enable_llm` does not reset that
model's learned quality history — it rehabilitates the normal way, through new ratings.

Check the current verdict on a live handle:

```python
llm = await llms.get("groq-llama")
print(llm.disabled)
```

Or read every model's raw facts at once:

```python
for name, entry in (await llms.snapshot()).items():
    print(name, entry.disabled, entry.has_key, entry.cooldown_until, entry.demoted_operations)
```

`snapshot()` returns one `LLMSnapshot` per model — `disabled`, `has_key`,
`cooldown_until`, `demoted_operations` (a tuple that may contain `None`, the bucket for
calls made without `operation=`), and `metrics` (call count, last status, last call
time) — raw facts, no status enum or precedence rule; you choose the presentation.

Read the call journal directly:

```python
calls = await llms.calls(limit=50)
```

## Tools & agents

`run_tool_loop` / `arun_tool_loop` handle the full back-and-forth: call the model,
execute the requested tools via `dispatch`, and repeat until a tool-call-free reply.

```python
import llmbroker

def get_weather(city: str) -> str:
    return f"It is 20°C in {city}"

tools = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Current weather in a city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}]

llms = llmbroker.Broker("llms.toml")
reply = llmbroker.run_tool_loop(
    llms,
    [{"role": "user", "content": "What is the weather in London?"}],
    tools=tools,
    dispatch={"get_weather": get_weather},
)
print(reply.text)
```

Async version: `await llmbroker.arun_tool_loop(...)`.

## Production

### Closing the broker

For one-off scripts you do not need to close the broker — the background thread
exits with the process.

Close the broker explicitly (`with`, or `try/finally` with `.close()`) if either
holds:

- **a long-lived process creates brokers repeatedly** (per request, in a loop) —
  otherwise each instance leaks a background thread;
- **an external service is wired in (Postgres, MongoDB)** — it holds a persistent
  connection that is only closed reliably on an explicit close.

```python
# Context manager — preferred
with llmbroker.Broker("llms.toml") as llms:
    reply = llms.ask("...")

# Or manually, when with is inconvenient
llms = llmbroker.Broker("llms.toml")
try:
    reply = llms.ask("...")
finally:
    llms.close()
```

`AsyncBroker` is the same via `async with` or `await llms.aclose()`.

### Choosing a data source

The broker's first positional argument dispatches on its form — one parameter picks
the registry, secrets, and store together:

```python
llmbroker.Broker("llms.toml")                    # file registry + env-var secrets + FileStore
llmbroker.Broker("broker.db")                     # sqlite backing all three ports
llmbroker.Broker("postgresql://host/db")          # postgres backing all three ports
llmbroker.Broker("mongodb://host/db")             # mongodb backing all three ports
```

An unrecognized form raises a clear error naming the accepted ones; a missing extra
(e.g. sqlite without `pip install llmbroker[sqlite]`) raises an actionable
`pip install llmbroker[...]` message. Override any port explicitly — an explicit
`registry=`/`secrets=`/`store=` always wins over whatever the source would have
supplied:

```python
import llmbroker
from llmbroker.postgres.registry import Registry as PostgresRegistry

pool = await asyncpg.create_pool(dsn)
async with llmbroker.AsyncBroker(
    registry=PostgresRegistry(pool),
    secrets=llmbroker.Secrets(),   # env vars instead of the DB
) as llms:
    reply = await llms.ask("Hello")
```

### Seeding a DB registry

A DB-backed registry starts empty; mirror a preset into it explicitly, once:

```python
llms = llmbroker.AsyncBroker(
    registry=llmbroker.sqlite.registry.Registry("broker.db"),
    secrets=llmbroker.sqlite.secrets.Secrets("broker.db"),
)
await llms.sync(llmbroker.Registry(".deploy/llms.toml"))  # once, e.g. at deploy
await llms.ensure_pool()   # eager init at startup
```

`sync(preset)` is a total mirror of the preset file: add new entries, update existing
ones, delete entries absent from the preset — nothing is lost by a delete, since keys
live in the secrets store and learned state derives from the journal (a model returning
to the preset later picks its old ratings and verdict back up). Changing an existing
entry's `model` under the same name is refused with an error — a model bump is meant to
be a new entry name, protecting the binding between a model's learned quality and its
name. Provisioning against an empty registry fails fast, telling you to call
`sync(preset)` first.

The same mirror is available from the CLI, for DB-init scripts:

```bash
python -m llmbroker sync llms.toml broker.db
python -m llmbroker sync llms.toml "postgresql://host/db"
```

### Journal retention

The call journal self-purges old records; every store backend takes a `retention`
constructor parameter (default 90 days):

```python
from datetime import timedelta

store = llmbroker.FileStore("store", retention=timedelta(days=30))
```

There is no separate purge call — retention is checked automatically on write
activity, at most once per hour.

A note on finicky providers: pass `parallel=1` on an `LLMConfig` entry to serialize
calls to one model — useful for providers that reject concurrent requests on the same
key.

## Multi-user

A multi-user host can give each end user their own API key over one shared registry
and store, via the opaque `scope: str | None` parameter (`""` is rejected — use `None`
for unscoped):

```python
import llmbroker

# App startup — shared infrastructure, one shared DB
async def handle_request(scope: str, prompt: str) -> str:
    async with llmbroker.AsyncBroker("broker.db", scope=scope) as llms:
        result = await llms.ask(prompt)
        return result.text
```

**The registry and everything the optimizer learns are always global** — one model
list, one set of quality windows and cooldowns, shared by every scope. There is no
per-tenant registry partition.

**Secrets are the one thing that is actually per-scope.** Key resolution tries
`resolve(f"{scope}/{api_key_ref}")` first, falling back to `resolve(api_key_ref)` — an
own key if the user set one, the shared key otherwise. The journal also carries `scope`
as a plain attribution field, filterable via `calls(...)` (learning itself stays
unscoped — a chatty scope's ratings feed the same shared quality windows as everyone
else's).

## AWS Secrets Manager backend

Install the extra first:

```bash
uv pip install "llmbroker[aws]"
```

```python
import llmbroker
from llmbroker.aws.secrets import Secrets as AwsSecrets

secrets = AwsSecrets(region_name="us-east-1")

async with llmbroker.AsyncBroker(
    registry=llmbroker.Registry("llms.toml"),
    secrets=secrets,
) as llms:
    reply = await llms.ask("Hello")
```

Secrets are stored under `{prefix}{ref}` in AWS Secrets Manager — `prefix` defaults to
`"llmbroker/"` and is configurable. `ref` already carries any `scope` prefix the broker
added, so no separate per-user path form exists on the backend itself.

## HashiCorp Vault backend

Install the extra first:

```bash
uv pip install "llmbroker[vault]"
```

```python
import llmbroker
from llmbroker.vault.secrets import Secrets as VaultSecrets

secrets = VaultSecrets(url="https://vault.example.com", token="s.xxx")

async with llmbroker.AsyncBroker(
    registry=llmbroker.Registry("llms.toml"),
    secrets=secrets,
) as llms:
    reply = await llms.ask("Hello")
```

KV v2 engine, path `llmbroker/{ref}`. The KV mount defaults to `"secret"` and is
configurable via `mount_point=`.

## Alembic integration

Add the hook so Alembic autogenerate ignores `llmbroker_*` tables:

```python
# alembic/env.py
import llmbroker.integrations.alembic

context.configure(
    connection=connection,
    target_metadata=target_metadata,
    include_object=llmbroker.integrations.alembic.include_object,
)
```

If you already have your own `include_object`, compose manually:

```python
def include_object(object, name, type_, reflected, compare_to):
    return (
        llmbroker.integrations.alembic.include_object(object, name, type_, reflected, compare_to)
        and your_predicate(object, name, type_, reflected, compare_to)
    )
```

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
# {"GROQ_API_KEY": KeyInfo(api_key_ref="GROQ_API_KEY", effort=EffortLevel.SIGNUP,
#                          value=ValueLevel.GOOD, help="Create a free API key at [groq](...) ..."), ...}
```

`key_info()` returns a `KeyInfo` (markdown `help`, plus `effort`/`value` for onboarding
sort order) per `api_key_ref`. It is an optional registry capability (`KeyInfoProtocol`
in `llmbroker.protocols.registry`): registries that carry the metadata expose it, others
do not — probe with `isinstance(registry, KeyInfoProtocol)` if you accept arbitrary
registries. It is independent of the broker, so you do not need to wire the registry as
a `seed=` to read it. Missing a key for some models is the normal way to run
llmbroker — the pool routes over whatever keys are present; only zero usable models
is an error.

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

The score is stored in telemetry (when using the SQLite backend) and, when the
optimizer is active (the default), folded into that model's learned quality profile —
see "Operations" and "Learned profile" below.

### Operations

Tag a call with the kind of task it is doing via `operation=`. Everything
quality-related — the decayed quality aggregate, per-operation demotions, and
`background_operations` ranking — is keyed by `(llm, operation)`, because a model's
usefulness is genuinely task-shaped: fine on simple tasks, weak on hard ones. Calls
made without `operation=` fall into one shared `None` bucket.

```python
reply = await llms.ask("Summarize this contract clause", operation="summarize")
reply.record_quality(0.9)  # rated against the "summarize" bucket specifically

# Background (non-interactive) work ranks by quality first, latency second
reply = await llms.ask("Nightly batch classification", operation="classify")
```

Mark an operation as background (batch/offline, not waiting on a human) by listing it
in `Optimizer(background_operations={"classify", ...})` — the ranking objective then
becomes `(-usable_rate, latency)` instead of the interactive default
`(latency, -usable_rate)`.

### Learned profile

When the optimizer is active, every rated call and every quality score is folded into
a durable, per-operation "how good is this model at this kind of task" profile that
survives restarts, preset updates, and (with a shared state store) is visible to every
instance in a cluster. A model whose measured quality is consistently poor for an
operation is **demoted** — moved to the back of the routing queue for that
operation — never silently excluded, because the quality signal is your own opinion
and may be miscalibrated. The only thing that actually excludes a model is the manual
bench below. See [`optimizer.md`](https://github.com/andgineer/llmbroker/blob/main/specs/reference/optimizer.md)
for the full mechanics (decayed aggregates, the decision band, tiered selection).

### Manual bench

```python
await llms.disable_llm("groq-llama", reason="hallucinating on our eval set")
# ... later ...
await llms.enable_llm("groq-llama")
```

`disable_llm` is the one verdict that actually excludes a model from routing —
covering every operation including future ones, surviving preset rolls, and never
overridden by the optimizer. `enable_llm` clears the latch and resets that model's
learned quality history, giving it a clean trial period.

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
- **an external service is wired in (Redis, Postgres)** — it holds a persistent
  connection that is only closed reliably on an explicit close.

```python
# Context manager — preferred
with llmbroker.Broker("llms.toml") as llms:
    reply = llms.ask("...")

# Or manually, when with is inconvenient
llms = llmbroker.Broker(registry=..., state_store=...)
try:
    reply = llms.ask("...")
finally:
    llms.close()
```

`AsyncBroker` is the same via `async with` or `await llms.aclose()`.

### SQLite backend: call history and pool management

```python
import llmbroker
import llmbroker.sqlite
from datetime import UTC, datetime

with llmbroker.Broker(
    registry=llmbroker.sqlite.Registry("broker.db"),
    telemetry=llmbroker.sqlite.Telemetry("broker.db"),
    seed="llms.toml",
    seed_policy=llmbroker.SeedPolicy.SYNC,  # the default — safe to omit
) as llms:
    reply = llms.ask("Question")

    # Pool status
    for name, entry in llms.snapshot().items():
        print(name, entry.state.phase, entry.metrics)

    # Inspect a single LLM
    llm = llms.get("groq-llama")
    print(llm.config, llm.state())

    # Count loaded LLMs
    print(llms.count())

    # Add / update / remove an LLM at runtime
    from llmbroker.models import LLMConfig
    llms.add(LLMConfig(
        name="new-llm",
        base_url="https://api.example.com/v1",
        model="gpt-4o-mini",
        api_key_ref="EXAMPLE_API_KEY",
    ))
    llms.update(LLMConfig(
        name="new-llm",
        base_url="https://api.example.com/v1",
        model="gpt-4o",
        api_key_ref="EXAMPLE_API_KEY",
    ))
    llms.remove("groq-gemma")

    # Call history
    calls = llms.calls(limit=50)
    llms.purge_calls(before=datetime(2025, 1, 1, tzinfo=UTC))
```

`SeedPolicy` values:

| Policy | Behaviour |
|---|---|
| `SeedPolicy.SYNC` (default) | curator-managed sync: adds new preset entries, updates their operational fields (`base_url`, `api_key_ref`, `metadata`), deprecates (never deletes) entries the preset has dropped, and never touches an entry you added yourself with `add()`. A preset row that changes an existing entry's `model` is refused with an alert instead of applied — a model bump is meant to be a new entry name, so old learned data is never silently reattributed. Never touches the learned profile. |
| `SeedPolicy.MIRROR` | DB = source exactly: add new, update changed, remove dropped — including that entry's learned profile. The one policy without a "never delete" guarantee, for callers who explicitly want pruning |
| `SeedPolicy.IF_EMPTY` | fill only if DB is empty, otherwise no-op |
| `SeedPolicy.ADD` | only add entries not already present by name |

### Multi-user (per-user scoping)

In a multi-user application each end user can have their own API keys and
optionally their own set of LLM entries, backed by a single shared database.

**Ports are app-lifetime infrastructure; the broker is constructed per request.**
Construct `registry`, `secrets`, `state_store`, and `telemetry` once at startup
and share them. Construct a new `AsyncBroker` (or `Broker`) for each request,
passing the user's id:

```python
import llmbroker
import llmbroker.sqlite

# App startup — shared infrastructure
registry   = llmbroker.sqlite.Registry("broker.db")
secrets    = llmbroker.sqlite.Secrets("broker.db")
telemetry  = llmbroker.sqlite.Telemetry("broker.db")
# state_store = <backend>  # required for stateless servers — see note below

# Per-request — cheap, single-tenant view
async def handle_request(user_id: str, prompt: str) -> str:
    async with llmbroker.AsyncBroker(
        registry=registry,
        secrets=secrets,
        telemetry=telemetry,
        # state_store=state_store,
        user_id=user_id,
    ) as llms:
        result = await llms.ask(prompt)
        return result.text
```

**Stateless servers need a `state_store`.**  In a process-per-request setup
(multiple workers, a load balancer, restarts) in-process cooldown state is
lost between requests, so a rate-limited LLM will appear available to the next
worker.  Pass a shared `state_store=` backend — Redis, Postgres, or any
implementation of `StateStoreProtocol` — to preserve cooldown state across
requests.  Backends are available as of the P3 release.

**All batteries** (registry, secrets, telemetry) scope records exactly to the
`user_id` passed. A broker with `user_id=None` (the default) sees and writes
only unscoped rows — reproducing today's single-tenant behavior. The same LLM
name can exist for multiple users independently.

**Optional paranoia guard** — `Secrets(require_user_id=True)` (and the SQLite
equivalent) raises `UserScopeError` if a broker calls `resolve` with
`user_id=None`. Use this when auth must always produce a real user id:

```python
from llmbroker import UserScopeError
import llmbroker.sqlite

secrets = llmbroker.sqlite.Secrets("broker.db", require_user_id=True)
```

### AWS Secrets Manager backend

Install the extra first:

```bash
uv pip install "llmbroker[aws]"
```

```python
import llmbroker
import llmbroker.aws

secrets = llmbroker.aws.Secrets(region_name="us-east-1")

async with llmbroker.AsyncBroker(
    registry=llmbroker.Registry("llms.toml"),
    secrets=secrets,
) as llms:
    reply = await llms.ask("Hello")
```

Secrets are stored under `llmbroker/{ref}` (single-tenant) or
`llmbroker/{ref}/{user_id}` (per-user) in AWS Secrets Manager.
The prefix defaults to `"llmbroker/"` and is configurable.
Pass `require_user_id=True` for the same paranoia guard.

### HashiCorp Vault backend

Install the extra first:

```bash
uv pip install "llmbroker[vault]"
```

```python
import llmbroker
import llmbroker.vault

secrets = llmbroker.vault.Secrets(url="https://vault.example.com", token="s.xxx")

async with llmbroker.AsyncBroker(
    registry=llmbroker.Registry("llms.toml"),
    secrets=secrets,
) as llms:
    reply = await llms.ask("Hello")
```

Secrets are stored at KV v2 paths `llmbroker/{ref}` (single-tenant) or
`llmbroker/users/{user_id}/{ref}` (per-user). The KV mount defaults to `"secret"`.
Pass `require_user_id=True` for the same paranoia guard.

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

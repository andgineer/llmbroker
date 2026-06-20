# Usage

## LLM pool configuration

Create `llms.toml` with a list of endpoints:

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

A ready-made list of free endpoints is available as
[freetier.toml](https://github.com/andgineer/llmbroker/blob/main/presets/freetier.toml)
in the repository.

`api_key_ref` is the name of the environment variable holding the key. To print
the required variable names for a config file:

```bash
python -m llmbroker env llms.toml
```

## Synchronous usage

```python
import llmbroker

llms = llmbroker.Broker(registry=llmbroker.Registry("llms.toml"))

# Single question
reply = llms.ask("Translate to French: Hello world")
print(reply.text)

# Full messages API
reply = llms.chat([
    {"role": "system", "content": "You are a concise assistant."},
    {"role": "user",   "content": "What is Python?"},
])
print(reply.text)
```

The synchronous `Broker` runs an internal event loop on a background thread —
convenient for scripts and synchronous applications.

## Asynchronous usage

```python
import llmbroker

async def main():
    async with llmbroker.AsyncBroker(
        registry=llmbroker.Registry("llms.toml"),
    ) as llms:
        reply = await llms.ask("What is asyncio?")
        print(reply.text)
```

`AsyncBroker` is the core engine; use it in FastAPI, agents, and background workers.

## Wait timeout

By default a call waits until any endpoint becomes free. To limit the wait:

```python
from llmbroker import NoLLMAvailableError

try:
    reply = llms.ask("Question", wait=5.0)   # at most 5 seconds
except NoLLMAvailableError:
    print("All LLMs are busy")
```

`wait=0` — fail immediately if no slot is free.

## Tool calls

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

llms = llmbroker.Broker(registry=llmbroker.Registry("llms.toml"))
reply = llmbroker.run_tool_loop(
    llms,
    [{"role": "user", "content": "What is the weather in London?"}],
    tools=tools,
    dispatch={"get_weather": get_weather},
)
print(reply.text)
```

Async version: `await llmbroker.arun_tool_loop(...)`.

## Quality feedback

```python
reply = llms.ask("Classify this receipt")
# ... inspect the result ...
reply.record_quality(1.0)   # good answer
reply.record_quality(0.0)   # bad answer
```

The score is stored in telemetry (when using the SQLite backend).

## SQLite backend: call history and pool management

```python
import llmbroker
import llmbroker.sqlite
from datetime import UTC, datetime

with llmbroker.Broker(
    registry=llmbroker.sqlite.Registry("broker.db"),
    telemetry=llmbroker.sqlite.Telemetry("broker.db"),
    seed=llmbroker.Registry("llms.toml"),
    seed_policy=llmbroker.SeedPolicy.IF_EMPTY,
) as llms:
    reply = llms.ask("Question")

    # Pool status
    for name, entry in llms.snapshot().items():
        print(name, entry.state.phase, entry.metrics)

    # Add / remove an endpoint at runtime
    from llmbroker.models import LLMConfig
    llms.add(LLMConfig(
        name="new-llm",
        base_url="https://api.example.com/v1",
        model="gpt-4o-mini",
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
| `SeedPolicy.MIRROR` | DB = source exactly: add new, update changed, remove dropped |
| `SeedPolicy.IF_EMPTY` (default) | fill only if DB is empty, otherwise no-op |
| `SeedPolicy.ADD` | only add entries not already present by name |

## Alembic integration

Add the hook so Alembic autogenerate ignores `llmbroker_*` tables:

```python
# alembic/env.py
import llmbroker.alembic

context.configure(
    connection=connection,
    target_metadata=target_metadata,
    include_object=llmbroker.alembic.include_object,
)
```

If you already have your own `include_object`, compose manually:

```python
def include_object(object, name, type_, reflected, compare_to):
    return (
        llmbroker.alembic.include_object(object, name, type_, reflected, compare_to)
        and your_predicate(object, name, type_, reflected, compare_to)
    )
```

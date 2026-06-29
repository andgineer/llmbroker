# llmbroker

Turn a crowd of free, rate-limited LLMs into one reliable model — no premium subscription, no single point of failure.

No heavy deps like LangChain.

## Quick start

Grab a ready-made pool, or write your own `llms.toml` (see [Usage](usage.md)):

```bash
llmbroker preset freetier > llms.toml
```

List the API keys it needs, get them from the providers, and set them:

```bash
llmbroker env llms.toml > .env
```

Each provider issues its own key (free-tier keys take about a minute to sign up for).
A `.env` file is the simplest option — secrets can also come from environment variables,
AWS, Vault, or any backend you plug in. Then call the broker:

```python
import llmbroker

llms = llmbroker.Broker("llms.toml")
print(llms.ask("Hello, how are you?").text)
```

If one LLM is rate-limited, the broker cools it down and switches to the next one.
The caller never sees a rate-limit error as long as at least one LLM is available.

## How it works

- Each LLM in the pool gets one queue slot: at most one in-flight request per LLM.
- When an LLM is rate-limited or unavailable, the broker puts it on cooldown and
  re-enqueues it after a delay (the provider's `Retry-After`, or 60 s by default).
- `ask(prompt)` is a shortcut for `chat([{"role": "user", "content": prompt}])`.
- If all LLMs are on cooldown and `wait` expires — `NoLLMAvailableError`.
- If an LLM was tried and returned an error — `AllLLMsFailedError`.

`AsyncBroker` is the core engine (FastAPI, agents, async workers); `Broker` is the
blocking wrapper shown above. See [Usage](usage.md) for both, tools, and multi-user.

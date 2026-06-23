# llmbroker

Route LLM calls over a **free LLM pool** with automatic round-robin and
cooldown.

No heavy deps like LangChain etc.

## Quick start

Create `llms.toml` with your LLMs, or download a ready-made preset:

```bash
llmbroker preset freetier > llms.toml
```

Example `llms.toml`:

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

`api_key_ref` is the name of the environment variable holding the key. The secret
never goes into the file.

List the keys your pool needs, then get them from the providers and set them:

```bash
llmbroker env llms.toml > .env
```

Each provider issues its own key (free-tier keys take about a minute to sign up for).
A `.env` file is the simplest option — secrets can also come from environment variables,
AWS, Vault, or any backend you plug in. With the keys set, call the broker:

```python
import llmbroker

llms = llmbroker.Broker("llms.toml")
print(llms.ask("Hello, how are you?").text)
```

If one endpoint returns 429, the broker cools it down and switches to the next one.
The caller never sees a rate-limit error as long as at least one endpoint is available.

## How it works

- Each LLM in the pool gets one queue slot: at most one in-flight request per endpoint.
- On 429/503 the broker puts the endpoint on cooldown and re-enqueues it after a
  delay (`Retry-After` header or 60 s by default).
- `ask(prompt)` is a shortcut for `chat([{"role": "user", "content": prompt}])`.
- If all endpoints are on cooldown and `wait` expires — `NoLLMAvailableError`.
- If an endpoint was tried and returned an error — `AllLLMsFailedError`.

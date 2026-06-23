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

Set your keys and call the broker:

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

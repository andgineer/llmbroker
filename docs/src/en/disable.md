# Disabling models

Take a model out of routing manually — completely, until you enable it back:

```python
broker.disable_llm("groq-llama")
# ... later ...
broker.enable_llm("groq-llama")
```

The verdict survives a preset `sync` and does not touch the model's accumulated
quality history.

A pool state snapshot shows who is disabled — one entry per model:

```python
for name, entry in broker.snapshot().items():
    print(name, entry.disabled, entry.has_key, entry.cooldown_until, entry.demoted_operations)
```

Fields — in [`LLMSnapshot`](reference.md#llmbroker.models.LLMSnapshot); the
pool-wide picture is in [Monitoring and the journal](monitoring.md#pool-health).

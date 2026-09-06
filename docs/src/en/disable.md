# Disabling models

You can remove a model from the pool manually and enable it again later:

```python
broker.disable_llm("groq-llama")
# ... later ...
broker.enable_llm("groq-llama")
```

The setting remains in effect after `sync` and does not change the model's
accumulated quality history.

Use `snapshot()` to inspect the current state of every model:

```python
for name, entry in broker.snapshot().items():
    print(name, entry.disabled, entry.has_key, entry.cooldown_until, entry.demoted_operations)
```

The fields are documented in
[`LLMSnapshot`](reference.md#llmbroker.models.LLMSnapshot). See
[Monitoring and the journal](monitoring.md#pool-health) for the pool-wide state.

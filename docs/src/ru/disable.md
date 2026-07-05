# Отключение моделей

Вывести модель из маршрутизации вручную — полностью, пока не включите обратно:

```python
llms.disable_llm("groq-llama")
# ... позже ...
llms.enable_llm("groq-llama")
```

Вердикт переживает `sync` пресета и не трогает накопленную историю качества
модели.

Снимок состояния пула — по одному элементу на модель:

```python
for name, entry in llms.snapshot().items():
    print(name, entry.disabled, entry.has_key, entry.cooldown_until, entry.demoted_operations)
```

Поля — в [`LLMSnapshot`](reference.md#llmbroker.models.LLMSnapshot).

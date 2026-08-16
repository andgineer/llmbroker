# Отключение моделей

Вывести модель из маршрутизации вручную — полностью, пока не включите обратно:

```python
broker.disable_llm("groq-llama")
# ... позже ...
broker.enable_llm("groq-llama")
```

Вердикт переживает `sync` пресета и не трогает накопленную историю качества
модели.

Кто сейчас выключен, видно в снимке пула — по одному элементу на модель:

```python
for name, entry in broker.snapshot().items():
    print(name, entry.disabled, entry.has_key, entry.cooldown_until, entry.demoted_operations)
```

Поля — в [`LLMSnapshot`](reference.md#llmbroker.models.LLMSnapshot), картина по
пулу целиком — в [Наблюдении и журнале](monitoring.md#pool-health).

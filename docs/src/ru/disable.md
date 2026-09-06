# Отключение моделей

Модель можно вручную исключить из пула, а затем включить обратно:

```python
broker.disable_llm("groq-llama")
# ... позже ...
broker.enable_llm("groq-llama")
```

Выбранное состояние сохраняется после `sync` и не влияет на накопленные оценки
качества модели.

Текущее состояние всех моделей можно получить через `snapshot()`:

```python
for name, entry in broker.snapshot().items():
    print(name, entry.disabled, entry.has_key, entry.cooldown_until, entry.demoted_operations)
```

Поля результата описаны в
[`LLMSnapshot`](reference.md#llmbroker.models.LLMSnapshot). Подробнее о состоянии
пула см. в разделе [Наблюдение и журнал](monitoring.md#pool-health).

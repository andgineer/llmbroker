# Асинхронность

`AsyncBroker` — основной движок; `Broker` — его блокирующая обёртка. Методы те
же, только с `await`:

```python
async with llmbroker.AsyncBroker("llms.toml") as llms:
    reply = await llms.ask("Привет")
    print(reply.text)
```

Асинхронный tool-цикл — `await llmbroker.arun_tool_loop(...)`, см.
[Инструменты и агенты](tools.md).

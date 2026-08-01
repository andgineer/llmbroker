# Асинхронность

`AsyncBroker` — основной движок; `Broker` — его блокирующая обёртка. Методы те
же, только с `await`:

```python
async with llmbroker.AsyncBroker("llms.toml") as llms:
    reply = await llms.ask("Привет")
    print(reply.text)
```

Стриминг — только асинхронный: пул отдаёт дельты по мере поступления, с
роутингом и фейловером:

```python
    async for delta in llms.stream("Напиши хокку про брокеров"):
        print(delta, end="", flush=True)
```

Что фейловер успевает спасти посреди стрима, а что нет — см.
[Прямые вызовы модели](direct.md).

Асинхронный tool-цикл — `await llmbroker.arun_tool_loop(...)`, см.
[Инструменты и агенты](tools.md).

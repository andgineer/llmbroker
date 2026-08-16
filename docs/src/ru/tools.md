# Инструменты и агенты

`run_tool_loop` берёт на себя весь цикл: вызывает модель, выполняет запрошенные
инструменты через `dispatch` и повторяет до финального ответа без tool-вызовов.

```python
def get_weather(city: str) -> str:
    return f"В {city} сейчас 20°C"

tools = [{
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Текущая погода в городе",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}]

llms = llmbroker.Broker()
reply = llmbroker.run_tool_loop(
    llms,
    [{"role": "user", "content": "Какая погода в Москве?"}],
    tools=tools,
    dispatch={"get_weather": get_weather},
)
print(reply.text, "— ответила", reply.llm_name)
```

Цикл возвращает результат последнего раунда — ровно такой же, как у `chat`:
текст, ответившую модель и `usage` этого раунда. Каждый предыдущий раунд был
самостоятельным вызовом со своей строкой в журнале, поэтому счётчики здесь — за
последний раунд, а не за весь цикл; сумму читайте по строкам журнала.

Асинхронная версия — `await llmbroker.arun_tool_loop(...)` поверх
[`AsyncBroker`](async.md).

Цикл ограничен `max_steps` (по умолчанию 8). Если модель и после последнего шага
просит инструменты, поднимается `llmbroker.ToolLoopLimitError`, а не возвращается
пустой ответ — увеличьте `max_steps` или перехватите исключение, чтобы забрать
то, что уже получилось.

Для ручного управления циклом `chat(messages, tools=...)` возвращает результат
с `tool_calls`.

# Использование

## Конфигурация пула LLM

Создайте файл `llms.toml` — список endpoint-ов:

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

Готовый список бесплатных endpoint-ов — [freetier.toml](https://github.com/andgineer/llmbroker/blob/main/presets/freetier.toml) в репозитории.

`api_key_ref` — имя переменной окружения с ключом. Получить список нужных переменных для конкретного файла:

```bash
python -m llmbroker env llms.toml
```

## Синхронное использование

```python
import llmbroker

llms = llmbroker.Broker("llms.toml")

# Один вопрос
reply = llms.ask("Переведи на английский: Привет мир")
print(reply.text)

# Полный messages API
reply = llms.chat([
    {"role": "system", "content": "Ты специалист по краткому обьяснению."},
    {"role": "user",   "content": "Что такое Python?"},
])
print(reply.text)
```

Синхронный `Broker` запускает внутренний цикл событий в фоновом потоке — удобен
для скриптов и синхронных приложений.

## Асинхронное использование

```python
import llmbroker

async def main():
    async with llmbroker.AsyncBroker("llms.toml") as llms:
        reply = await llms.ask("Что такое asyncio?")
        print(reply.text)
```

`AsyncBroker` — основной движок; используйте его в FastAPI, агентах и фоновых
воркерах.

## Закрытие брокера

Для разовых скриптов закрывать брокер не нужно — фоновый поток завершится вместе
с процессом.

Закрывайте брокер явно (`with` или `try/finally` с `.close()`), если выполняется
хотя бы одно условие:

- **долгоживущий процесс создаёт брокеры повторно** (в обработчике запроса,
  в цикле) — иначе на каждый экземпляр утекает фоновый поток;
- **подключён внешний сервис (Redis, Postgres)** — у него постоянное соединение,
  которое надёжно закрывается только явно.

```python
# Контекстный менеджер — предпочтительно
with llmbroker.Broker("llms.toml") as llms:
    reply = llms.ask("...")

# Или вручную, когда with неудобен
llms = llmbroker.Broker(registry=..., shared_state=...)
try:
    reply = llms.ask("...")
finally:
    llms.close()
```

`AsyncBroker` — то же самое через `async with` или `await llms.aclose()`.

## Таймаут ожидания свободного слота

По умолчанию вызов ждёт, пока освободится любой endpoint. Чтобы ограничить
ожидание:

```python
from llmbroker import NoLLMAvailableError

try:
    reply = llms.ask("Вопрос", wait=5.0)   # максимум 5 секунд
except NoLLMAvailableError:
    print("Все LLM заняты")
```

`wait=0` — немедленный отказ, если нет свободного слота.

## Инструменты (tool calls)

`run_tool_loop` / `arun_tool_loop` берут на себя весь цикл: вызывают модель,
выполняют запрошенные инструменты через `dispatch` и повторяют до получения
финального ответа без tool-вызовов.

```python
import llmbroker

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

llms = llmbroker.Broker("llms.toml")
reply = llmbroker.run_tool_loop(
    llms,
    [{"role": "user", "content": "Какая погода в Москве?"}],
    tools=tools,
    dispatch={"get_weather": get_weather},
)
print(reply.text)
```

Асинхронная версия — `await llmbroker.arun_tool_loop(...)`.

## Оценка качества ответа

```python
reply = llms.ask("Классифицируй чек")
# ... проверяем результат ...
reply.record_quality(1.0)   # хороший ответ
reply.record_quality(0.0)   # неудачный
```

Оценка сохраняется в телеметрию (при использовании SQLite-бэкенда).

## SQLite-бэкенд: история вызовов и управление пулом

```python
import llmbroker
import llmbroker.sqlite
from datetime import UTC, datetime

with llmbroker.Broker(
    registry=llmbroker.sqlite.Registry("broker.db"),
    telemetry=llmbroker.sqlite.Telemetry("broker.db"),
    seed="llms.toml",
    seed_policy=llmbroker.SeedPolicy.IF_EMPTY,
) as llms:
    reply = llms.ask("Вопрос")

    # Состояние пула
    for name, entry in llms.snapshot().items():
        print(name, entry.state.phase, entry.metrics)

    # Добавить / удалить endpoint во время работы
    from llmbroker.models import LLMConfig
    llms.add(LLMConfig(
        name="new-llm",
        base_url="https://api.example.com/v1",
        model="gpt-4o-mini",
        api_key_ref="EXAMPLE_API_KEY",
    ))
    llms.remove("groq-gemma")

    # История вызовов
    calls = llms.calls(limit=50)
    llms.purge_calls(before=datetime(2025, 1, 1, tzinfo=UTC))
```

Значения `SeedPolicy`:

| Политика | Поведение |
|---|---|
| `SeedPolicy.MIRROR` | DB = источник точно: добавить новые, обновить изменённые, удалить удалённые |
| `SeedPolicy.IF_EMPTY` (по умолчанию) | заполнить только если DB пуста, иначе ничего |
| `SeedPolicy.ADD` | только добавить новые по имени, существующие не трогать |

## Интеграция с Alembic

Подключите хук, чтобы автогенерация миграций игнорировала таблицы `llmbroker_*`:

```python
# alembic/env.py
import llmbroker.alembic

context.configure(
    connection=connection,
    target_metadata=target_metadata,
    include_object=llmbroker.alembic.include_object,
)
```

Если есть свой `include_object`, скомпонуйте вручную:

```python
def include_object(object, name, type_, reflected, compare_to):
    return (
        llmbroker.alembic.include_object(object, name, type_, reflected, compare_to)
        and your_predicate(object, name, type_, reflected, compare_to)
    )
```

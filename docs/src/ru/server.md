# Серверы и кластеры

Тот же брокер масштабируется на несколько процессов и хостов: переключите его с
файла на общую БД — вызывающий код не меняется.

## Общая БД {#datasource}

Первый аргумент брокера задаёт сразу пул моделей, ключи и журнал:

```python
llmbroker.Broker("llms.toml")               # файлы + ключи из окружения
llmbroker.Broker("broker.db")               # sqlite
llmbroker.Broker("postgresql://host/db")    # postgres
llmbroker.Broker("mongodb://host/db")       # mongodb
```

Каждому варианту нужен свой extra — см. [Установка](installation.md). Любую
часть можно переопределить явно через `registry=` / `secrets=` / `store=`.

БД стартует пустой — залейте в неё пресет один раз, например при деплое:

```bash
llmbroker sync llms.toml "postgresql://host/db"
```

Повторный `sync` — полная синхронизация с файлом: добавляет, обновляет и удаляет
записи; при удалении накопленная история модели не теряется. То же из кода:
`await llms.sync(llmbroker.Registry("llms.toml"))`.

## Закрытие брокера {#closing}

Закрывайте брокер явно, если долгоживущий процесс создаёт брокеры повторно или
подключена внешняя БД:

```python
with llmbroker.Broker("broker.db") as llms:
    reply = llms.ask("...")
```

`AsyncBroker` — `async with` или `await llms.aclose()`.

## Журнал вызовов

Журнал самоочищается; глубина хранения — параметр `retention` бэкенда журнала
(по умолчанию 90 дней). Прочитать: `llms.calls(limit=50)`.

## Свой ключ на пользователя {#multiuser}

`scope=` даёт каждому пользователю свой API-ключ поверх общего пула:

```python
async with llmbroker.AsyncBroker("broker.db", scope=user_id) as llms:
    reply = await llms.ask(prompt)
```

Ключ ищется сначала по скоупу пользователя, потом общий. Пул моделей и всё, чему
он учится, общие для всех; журнал хранит `scope` — по нему можно фильтровать
`calls(...)`.

## Alembic

Чтобы автогенерация миграций игнорировала таблицы `llmbroker_*`:

```python
# alembic/env.py
import llmbroker.integrations.alembic

context.configure(
    connection=connection,
    target_metadata=target_metadata,
    include_object=llmbroker.integrations.alembic.include_object,
)
```

Свой `include_object` скомпонуйте с ним через `and`.

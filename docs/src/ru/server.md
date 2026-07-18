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

## SQLite: общая база и WAL {#sqlite}

Штатный режим — одна база, общая для llmbroker и вашего приложения: брокер держит
свои таблицы `llmbroker_*` рядом с вашими и больше ничего не трогает (хук
[Alembic](#alembic) убирает их из автогенерации миграций).

llmbroker никогда не устанавливает и не меняет `journal_mode` SQLite — WAL это
персистентное свойство уровня файла, принадлежащее владельцу файла БД, поэтому
включать его — ваша задача, не брокера. На общем файле владелец — ваше
приложение: включите WAL там, если нужна конкурентность чтения и записи.

Если приложение активно пишет в этот файл, дайте брокеру отдельный файл —
укажите отдельный `.db`, и они перестанут конкурировать за файловый лок SQLite.
Файл, принадлежащий только брокеру, вы настраиваете напрямую; WAL ставится один
раз и сохраняется:

```bash
sqlite3 broker.db 'PRAGMA journal_mode=WAL'
```

Это касается только SQLite. У Postgres и MongoDB такого файлового лока нет —
общая база с приложением нормальна, а отдельная схема или база — опциональная
аккуратность, не потребность конкурентности.

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

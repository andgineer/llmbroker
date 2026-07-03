# Использование

## Конфигурация

Создайте файл `llms.toml` — список LLM:

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

Готовый список бесплатных LLM доступен как пресет:

```bash
llmbroker preset freetier > llms.toml
```

`api_key_ref` — имя переменной окружения с ключом. Получить список нужных переменных для конкретного файла:

```bash
llmbroker env llms.toml
```

Это только имена переменных — сами ключи нужно получить у каждого провайдера и установить.
`.env`-файл — самый простой путь, но ключи могут браться из любого Secrets бэкенда (окружение, DB,
AWS, Vault, …).

### Где взять ключи

Если в конфиге есть таблица `[keys]` — как в готовых пресетах — `llmbroker env`
печатает над каждой переменной комментарий, где взять этот ключ:

```
# GROQ_API_KEY — Create a free API key at [groq](https://console.groq.com/keys) (sign in, then New API Key).
GROQ_API_KEY=
```

Чтобы показать те же подсказки в своём UI, прочитайте их программно из файлового реестра:

```python
import asyncio
import llmbroker

registry = llmbroker.Registry("llms.toml")
info = asyncio.run(registry.key_info())
# {"GROQ_API_KEY": KeyInfo(api_key_ref="GROQ_API_KEY", effort=EffortLevel.SIGNUP,
#                          value=ValueLevel.GOOD, help="Create a free API key at [groq](...) ..."), ...}
```

`key_info()` возвращает `KeyInfo` (markdown `help`, а также `effort`/`value` для
сортировки при онбординге) на каждый `api_key_ref`. Это опциональная возможность реестра
(`KeyInfoProtocol` в `llmbroker.protocols.registry`): реестры с такими данными её
предоставляют, остальные — нет; проверяйте через `isinstance(registry, KeyInfoProtocol)`,
если принимаете произвольные реестры. Она не зависит от брокера — реестр не нужно
передавать как `seed=`, чтобы прочитать подсказки. Отсутствие ключа для части моделей —
нормальный режим работы llmbroker: пул маршрутизирует по тем ключам, что есть; ошибка —
только если рабочих моделей не осталось совсем.

## Вызов брокера

`AsyncBroker` — основной движок; используйте его в FastAPI, агентах и асинхронных
воркерах:

```python
import llmbroker

async def main():
    async with llmbroker.AsyncBroker("llms.toml") as llms:
        # Один вопрос
        reply = await llms.ask("Переведи на английский: Привет мир")
        print(reply.text)

        # Полный messages API
        reply = await llms.chat([
            {"role": "system", "content": "Ты специалист по краткому объяснению."},
            {"role": "user",   "content": "Что такое Python?"},
        ])
        print(reply.text)
```

### Синхронная обёртка

`Broker` оборачивает тот же движок в блокирующий API — для скриптов и синхронных
приложений. Он запускает внутренний цикл событий в фоновом потоке; методы те же,
только без `await`:

```python
import llmbroker

llms = llmbroker.Broker("llms.toml")
print(llms.ask("Переведи на английский: Привет мир").text)
```

## Управление запросами

### Таймаут ожидания свободного слота

По умолчанию вызов ждёт, пока освободится любой LLM. Чтобы ограничить ожидание:

```python
from llmbroker import NoLLMAvailableError

try:
    reply = llms.ask("Вопрос", wait=5.0)   # максимум 5 секунд
except NoLLMAvailableError:
    print("Все LLM заняты")
```

`wait=0` — немедленный отказ, если нет свободного слота.

### Оценка качества ответа

```python
reply = llms.ask("Классифицируй как позитивный или негативный: 'Быстрая доставка, отличная упаковка!'")
# ... проверяем результат ...
reply.record_quality(1.0)   # хороший ответ
reply.record_quality(0.0)   # неудачный
```

Оценка сохраняется в телеметрию (при использовании SQLite-бэкенда) и, если активен
оптимизатор (по умолчанию так и есть), попадает в накопленный профиль качества этой
модели — см. «Операции» и «Накопленный профиль» ниже.

### Операции

Пометьте вызов видом задачи через `operation=`. Всё, что связано с качеством —
затухающая агрегата качества, демоции по операциям и ранжирование
`background_operations` — привязано к паре `(llm, operation)`, потому что полезность
модели действительно зависит от задачи: на простых задачах модель может быть хороша,
на сложных — слаба. Вызовы без `operation=` попадают в общий bucket `None`.

```python
reply = await llms.ask("Кратко перескажи этот пункт договора", operation="summarize")
reply.record_quality(0.9)  # оценка попадает именно в bucket "summarize"

# Фоновая (не интерактивная) работа ранжируется сначала по качеству, потом по задержке
reply = await llms.ask("Ночная пакетная классификация", operation="classify")
```

Пометьте операцию как фоновую (пакетную/офлайн, не ожидающую человека), перечислив
её в `Optimizer(background_operations={"classify", ...})` — тогда цель ранжирования
меняется с интерактивной по умолчанию `(latency, -usable_rate)` на
`(-usable_rate, latency)`.

### Накопленный профиль

Когда активен оптимизатор, каждый вызов и каждая оценка качества попадают в
долговечный, привязанный к операции профиль «насколько хороша эта модель для такого
рода задач», который переживает перезапуски, обновления пресета и (при общем
state store) виден всем экземплярам в кластере. Модель, чьё измеренное качество для
какой-то операции стабильно низкое, **демотируется** — переносится в конец очереди
маршрутизации для этой операции — но никогда не исключается молча, потому что сигнал
качества — это ваше собственное мнение и оно может быть неточным. Единственное, что
реально исключает модель — ручная блокировка ниже. Полная механика (затухающие
агрегаты, «мёртвая зона» решения, многоуровневый выбор) — в
[`optimizer.md`](https://github.com/andgineer/llmbroker/blob/main/specs/reference/optimizer.md).

### Ручная блокировка модели

```python
await llms.disable_llm("groq-llama", reason="галлюцинирует на нашем eval-наборе")
# ... позже ...
await llms.enable_llm("groq-llama")
```

`disable_llm` — единственный вердикт, который реально исключает модель из
маршрутизации: покрывает все операции, включая будущие, переживает обновления
пресета и никогда не переопределяется оптимизатором. `enable_llm` снимает блокировку
и сбрасывает накопленную историю качества этой модели — чистый пробный период.

## Инструменты и агенты

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

## Продакшен

### Закрытие брокера

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
llms = llmbroker.Broker(registry=..., state_store=...)
try:
    reply = llms.ask("...")
finally:
    llms.close()
```

`AsyncBroker` — то же самое через `async with` или `await llms.aclose()`.

### SQLite-бэкенд: история вызовов и управление пулом

```python
import llmbroker
import llmbroker.sqlite
from datetime import UTC, datetime

with llmbroker.Broker(
    registry=llmbroker.sqlite.Registry("broker.db"),
    telemetry=llmbroker.sqlite.Telemetry("broker.db"),
    seed="llms.toml",
    seed_policy=llmbroker.SeedPolicy.SYNC,  # значение по умолчанию — можно не указывать
) as llms:
    reply = llms.ask("Вопрос")

    # Состояние пула
    for name, entry in llms.snapshot().items():
        print(name, entry.state.phase, entry.metrics)

    # Получить один LLM
    llm = llms.get("groq-llama")
    print(llm.config, llm.state())

    # Количество загруженных LLM
    print(llms.count())

    # Добавить / обновить / удалить LLM во время работы
    from llmbroker.models import LLMConfig
    llms.add(LLMConfig(
        name="new-llm",
        base_url="https://api.example.com/v1",
        model="gpt-4o-mini",
        api_key_ref="EXAMPLE_API_KEY",
    ))
    llms.update(LLMConfig(
        name="new-llm",
        base_url="https://api.example.com/v1",
        model="gpt-4o",
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
| `SeedPolicy.SYNC` (по умолчанию) | синхронизация под управлением куратора: добавляет новые записи пресета, обновляет их операционные поля (`base_url`, `api_key_ref`, `metadata`), депрекейтит (никогда не удаляет) записи, пропавшие из пресета, и никогда не трогает запись, добавленную вами через `add()`. Если строка пресета меняет `model` у существующей записи — изменение отклоняется с алертом вместо применения: смена модели должна быть новым именем записи, чтобы старые накопленные данные никогда не были молча переприписаны. Накопленный профиль не трогает никогда |
| `SeedPolicy.MIRROR` | DB = источник точно: добавить новые, обновить изменённые, удалить удалённые — включая накопленный профиль этой записи. Единственная политика без гарантии «никогда не удалять», для тех, кому нужна именно чистка |
| `SeedPolicy.IF_EMPTY` | заполнить только если DB пуста, иначе ничего |
| `SeedPolicy.ADD` | только добавить новые по имени, существующие не трогать |

### Мультипользовательский режим (per-user scoping)

В многопользовательском приложении у каждого пользователя могут быть свои
API-ключи и свой набор LLM-записей — при этом используется одна общая база данных.

**Порты создаются один раз при старте приложения; брокер создаётся на каждый запрос.**
Создайте `registry`, `secrets`, `state_store` и `telemetry` один раз и
передавайте их по ссылке. Для каждого запроса создавайте новый `AsyncBroker` (или
`Broker`) с идентификатором пользователя:

```python
import llmbroker
import llmbroker.sqlite

# Старт приложения — общая инфраструктура
registry   = llmbroker.sqlite.Registry("broker.db")
secrets    = llmbroker.sqlite.Secrets("broker.db")
telemetry  = llmbroker.sqlite.Telemetry("broker.db")
# state_store = <backend>  # нужен для stateless-серверов — см. примечание ниже

# На каждый запрос — дешёвый экземпляр для одного пользователя
async def handle_request(user_id: str, prompt: str) -> str:
    async with llmbroker.AsyncBroker(
        registry=registry,
        secrets=secrets,
        telemetry=telemetry,
        # state_store=state_store,
        user_id=user_id,
    ) as llms:
        result = await llms.ask(prompt)
        return result.text
```

**Stateless-серверам нужен `state_store`.**  В схеме «один процесс на запрос»
(несколько воркеров, балансировщик, перезапуски) внутрипроцессное состояние
cooldown теряется между запросами — следующий воркер не знает, что LLM
заблокирован по rate limit.  Передайте общий `state_store=` (Redis, Postgres
или любую реализацию `StateStoreProtocol`), чтобы cooldown сохранялся между
запросами.  Бэкенды доступны начиная с релиза P3.

**Все батареи** (реестр, секреты, телеметрия) привязывают записи точно к
переданному `user_id`. Брокер с `user_id=None` (по умолчанию) видит и пишет
только строки без скоупа — воспроизводя текущее однопользовательское поведение.
Одно и то же имя LLM может существовать у нескольких пользователей независимо.

**Опциональная защита** — `Secrets(require_user_id=True)` (и SQLite-аналог)
выбрасывает `UserScopeError`, если брокер вызывает `resolve` с `user_id=None`:

```python
from llmbroker import UserScopeError
import llmbroker.sqlite

secrets = llmbroker.sqlite.Secrets("broker.db", require_user_id=True)
```

### Бэкенд AWS Secrets Manager

Сначала установите extra:

```bash
uv pip install "llmbroker[aws]"
```

```python
import llmbroker
import llmbroker.aws

secrets = llmbroker.aws.Secrets(region_name="us-east-1")

async with llmbroker.AsyncBroker(
    registry=llmbroker.Registry("llms.toml"),
    secrets=secrets,
) as llms:
    reply = await llms.ask("Привет")
```

Секреты хранятся в AWS Secrets Manager по именам `llmbroker/{ref}` (однопользовательский
режим) или `llmbroker/{ref}/{user_id}` (мультипользовательский). Префикс по умолчанию
`"llmbroker/"`, он настраивается. Поддерживает `require_user_id=True`.

### Бэкенд HashiCorp Vault

Сначала установите extra:

```bash
uv pip install "llmbroker[vault]"
```

```python
import llmbroker
import llmbroker.vault

secrets = llmbroker.vault.Secrets(url="https://vault.example.com", token="s.xxx")

async with llmbroker.AsyncBroker(
    registry=llmbroker.Registry("llms.toml"),
    secrets=secrets,
) as llms:
    reply = await llms.ask("Привет")
```

Секреты хранятся в KV v2 по путям `llmbroker/{ref}` (однопользовательский режим) или
`llmbroker/users/{user_id}/{ref}` (мультипользовательский). Точка монтирования KV по
умолчанию `"secret"`. Поддерживает `require_user_id=True`.

## Интеграция с Alembic

Подключите хук, чтобы автогенерация миграций игнорировала таблицы `llmbroker_*`:

```python
# alembic/env.py
import llmbroker.integrations.alembic

context.configure(
    connection=connection,
    target_metadata=target_metadata,
    include_object=llmbroker.integrations.alembic.include_object,
)
```

Если есть свой `include_object`, скомпонуйте вручную:

```python
def include_object(object, name, type_, reflected, compare_to):
    return (
        llmbroker.integrations.alembic.include_object(object, name, type_, reflected, compare_to)
        and your_predicate(object, name, type_, reflected, compare_to)
    )
```

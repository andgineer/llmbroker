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

Условия бесплатных тарифов постоянно меняются, поэтому пресет периодически
обновляется из актуальных источников — см. `presets/freetier-refresh-prompt.md`
(команда `invoke catalog-refresh` печатает его) и
[`freetier-providers.md`](https://github.com/andgineer/llmbroker/blob/main/specs/reference/freetier-providers.md) —
документ, который этот промпт читает и обновляет.

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
# {"GROQ_API_KEY": KeyInfo(api_key_ref="GROQ_API_KEY", help="Create a free API key at [groq](...) ...",
#                          extra={"effort": "signup", "value": "good"}), ...}
```

`key_info()` возвращает `KeyInfo` (markdown `help`, а также произвольный проброс
`extra: dict[str, str]` — всё остальное, что лежит в TOML-секции `[keys.REF]`;
у llmbroker нет своей таксономии на этот счёт) на каждый `api_key_ref`. Это
опциональная возможность реестра (`KeyInfoProtocol` в `llmbroker.protocols.registry`):
реестры с такими данными её предоставляют, остальные — нет; проверяйте через
`isinstance(registry, KeyInfoProtocol)`, если принимаете произвольные реестры. Она не
зависит от брокера — реестр не нужно подключать к брокеру, чтобы прочитать подсказки.
Отсутствие ключа для части моделей — нормальный режим работы llmbroker: пул
маршрутизирует по тем ключам, что есть; ошибка — только если рабочих моделей не
осталось совсем.

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

Оценка попадает в журнал как самостоятельная, отдельная запись (никогда не
привязывается обратно к самому вызову) и, если активен оптимизатор (по умолчанию так
и есть), питает окно качества этой модели по операции — см. «Обучение и выбор
модели» ниже.

### Операции

Пометьте вызов видом задачи через `operation=`. Оценки качества и демоции привязаны
к паре `(llm, operation)`, потому что полезность модели действительно зависит от
задачи: на простых задачах модель может быть хороша, на сложных — слаба. Вызовы без
`operation=` попадают в один общий bucket (ключ `None`).

```python
reply = await llms.ask("Кратко перескажи этот пункт договора", operation="summarize")
reply.record_quality(0.9)  # оценка попадает именно в bucket "summarize"
```

## Обучение и выбор модели

Когда оптимизатор активен (`optimize=True`, по умолчанию), каждая оценённая
метрика попадает в скользящее окно последних `quality_window` (по умолчанию 30)
оценок для пары `(модель, операция)`. Bucket **демотируется**, как только в нём
накопится не менее `quality_min_count` оценок (по умолчанию 10), а верхняя граница
Уилсона по ним опустится ниже `quality_floor` (по умолчанию 0.3).

Выбор — это одна сортировка: в рамках запрошенной операции демотированная модель
уходит в конец списка после всех недемотированных; среди слотов с одинаковым
вердиктом демоции побеждает порядок из реестра/пресета (чем ниже позиция, тем
лучше). Демоция всегда мягкая — если в пуле остались только демотированные модели,
запрос всё равно обслуживается, потому что сигнал качества — это ваше собственное
мнение и оно может быть неточным. Общего вердикта «плохая модель» не существует:
одна и та же модель может быть демотирована для `"classify"` и в полном порядке для
`"summarize"`.

Восстановление — это ровно новые оценки, вытесняющие окно: как только граница
снова поднимается выше порога, демоция снимается. Нет восстановления по времени,
нет пробного трафика и нет отдельного вызова «сбросить качество» — единственный
способ обновить накопленное качество модели — продолжать присылать ей оценки.

Это накопленное состояние (окна оценок, общие 429/503 cooldown, флипы демоции)
выводится из журнала вызовов и перечитывается по debounce — где оно хранится, см.
«Продакшен» ниже. Полная механика — в
[`optimizer.md`](https://github.com/andgineer/llmbroker/blob/main/specs/reference/optimizer.md).

## Администрирование

`disable_llm` — это ручной, жёсткий вердикт, отдельный от (и сильнее, чем) демоция
по качеству:

```python
await llms.disable_llm("groq-llama")
# ... позже ...
await llms.enable_llm("groq-llama")
```

`disable_llm` полностью выводит модель из маршрутизации — по всем операциям,
включая будущие, — переживает синхронизацию пресета, пока `enable_llm` не снимет
вердикт. Значение хранится в файле `store/disabled.yml` стора (или в аналогичной
таблице БД), который можно редактировать вручную, минуя брокер. `enable_llm` не
сбрасывает накопленную историю качества модели — она восстанавливается обычным
образом, через новые оценки.

Проверить текущий вердикт через живой хендл:

```python
llm = await llms.get("groq-llama")
print(llm.disabled)
```

Или прочитать сырые факты по всем моделям сразу:

```python
for name, entry in (await llms.snapshot()).items():
    print(name, entry.disabled, entry.has_key, entry.cooldown_until, entry.demoted_operations)
```

`snapshot()` возвращает по одному `LLMSnapshot` на модель — `disabled`, `has_key`,
`cooldown_until`, `demoted_operations` (кортеж, который может содержать `None` —
bucket для вызовов без `operation=`) и `metrics` (число вызовов, последний статус,
время последнего вызова) — сырые факты без статусного enum или правила
приоритета; представление выбираете вы сами.

Прочитать журнал вызовов напрямую:

```python
calls = await llms.calls(limit=50)
```

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
- **подключён внешний сервис (Postgres, MongoDB)** — у него постоянное соединение,
  которое надёжно закрывается только явно.

```python
# Контекстный менеджер — предпочтительно
with llmbroker.Broker("llms.toml") as llms:
    reply = llms.ask("...")

# Или вручную, когда with неудобен
llms = llmbroker.Broker("llms.toml")
try:
    reply = llms.ask("...")
finally:
    llms.close()
```

`AsyncBroker` — то же самое через `async with` или `await llms.aclose()`.

### Выбор источника данных

Первый позиционный аргумент брокера определяет диспетчеризацию по своей форме —
один параметр задаёт сразу реестр, секреты и стор:

```python
llmbroker.Broker("llms.toml")                    # файловый реестр + секреты из окружения + FileStore
llmbroker.Broker("broker.db")                     # sqlite для всех трёх портов
llmbroker.Broker("postgresql://host/db")          # postgres для всех трёх портов
llmbroker.Broker("mongodb://host/db")             # mongodb для всех трёх портов
```

Нераспознанная форма выбрасывает понятную ошибку с перечислением допустимых
вариантов; отсутствующий extra (например, sqlite без
`pip install llmbroker[sqlite]`) — понятную ошибку вида
`pip install llmbroker[...]`. Любой порт можно переопределить явно — явные
`registry=`/`secrets=`/`store=` всегда побеждают то, что предложил бы источник:

```python
import llmbroker
from llmbroker.postgres import Registry as PostgresRegistry

pool = await asyncpg.create_pool(dsn)
async with llmbroker.AsyncBroker(
    registry=PostgresRegistry(pool),
    secrets=llmbroker.Secrets(),   # секреты из окружения вместо БД
) as llms:
    reply = await llms.ask("Hello")
```

### Заполнение БД-реестра

БД-реестр стартует пустым; синхронизируйте его с пресетом явно, один раз:

```python
from llmbroker.sqlite import Registry as SqliteRegistry
from llmbroker.sqlite import Secrets as SqliteSecrets

llms = llmbroker.AsyncBroker(
    registry=SqliteRegistry("broker.db"),
    secrets=SqliteSecrets("broker.db"),
)
await llms.sync(llmbroker.Registry(".deploy/llms.toml"))  # один раз, например при деплое
await llms.ensure_pool()   # немедленная инициализация при старте
```

`sync(preset)` — полная синхронизация с файлом пресета: добавляет новые записи,
обновляет существующие, удаляет отсутствующие в пресете — при удалении ничего не
теряется, потому что ключи живут в secrets, а накопленное состояние выводится из
журнала (если модель позже вернётся в пресет, её старые оценки и вердикт
подхватятся снова). Изменение `model` у существующей записи с тем же именем
отклоняется с ошибкой — смена модели должна быть новым именем записи, это защищает
связь между накопленным качеством модели и её именем. Провижининг над пустым
реестром сразу завершается ошибкой и просит сначала вызвать `sync(preset)`.

Та же синхронизация доступна из CLI — для скриптов инициализации БД:

```bash
python -m llmbroker sync llms.toml broker.db
python -m llmbroker sync llms.toml "postgresql://host/db"
```

### Хранение журнала

Журнал вызовов самоочищается; каждый бэкенд стора принимает параметр конструктора
`retention` (по умолчанию 90 дней):

```python
from datetime import timedelta

store = llmbroker.FileStore("store", retention=timedelta(days=30))
```

Отдельного вызова для очистки нет — retention проверяется автоматически при
активности записи, не чаще раза в час.

Замечание про капризных провайдеров: укажите `parallel=1` у записи `LLMConfig`,
чтобы сериализовать вызовы к одной модели — полезно для провайдеров, не терпящих
параллельных запросов с одним ключом.

## Мультипользовательский режим

Многопользовательское приложение может дать каждому пользователю свой API-ключ
поверх одного общего реестра и стора — через непрозрачный параметр
`scope: str | None` (`""` запрещена — для отсутствия скоупа используйте `None`):

```python
import llmbroker

# Старт приложения — общая инфраструктура, одна общая БД
async def handle_request(scope: str, prompt: str) -> str:
    async with llmbroker.AsyncBroker("broker.db", scope=scope) as llms:
        result = await llms.ask(prompt)
        return result.text
```

**Реестр и всё, чему учится оптимизатор, всегда общие** — один список моделей,
одни окна качества и cooldown на все скоупы. Партиционирования реестра по
пользователям не существует.

**По-настоящему по-скоупово устроены только секреты.** Разрешение ключа сначала
пробует `resolve(f"{scope}/{api_key_ref}")`, а при неудаче — `resolve(api_key_ref)`:
свой ключ, если пользователь его завёл, иначе общий. Журнал также несёт `scope`
как обычное поле атрибуции, доступное для фильтрации через `calls(...)` (само
обучение остаётся неразделённым по скоупам — оценки болтливого скоупа питают те
же общие окна качества, что и у всех остальных).

## Бэкенд AWS Secrets Manager

Сначала установите extra:

```bash
uv pip install "llmbroker[aws]"
```

```python
import llmbroker
from llmbroker.aws import Secrets as AwsSecrets

secrets = AwsSecrets(region_name="us-east-1")

async with llmbroker.AsyncBroker(
    registry=llmbroker.Registry("llms.toml"),
    secrets=secrets,
) as llms:
    reply = await llms.ask("Привет")
```

Секреты хранятся в AWS Secrets Manager по именам `{prefix}{ref}` — `prefix` по
умолчанию `"llmbroker/"` и настраивается. `ref` уже несёт любой префикс скоупа,
который добавил брокер, поэтому отдельной формы пути «на пользователя» в самом
бэкенде нет.

## Бэкенд HashiCorp Vault

Сначала установите extra:

```bash
uv pip install "llmbroker[vault]"
```

```python
import llmbroker
from llmbroker.vault import Secrets as VaultSecrets

secrets = VaultSecrets(url="https://vault.example.com", token="s.xxx")

async with llmbroker.AsyncBroker(
    registry=llmbroker.Registry("llms.toml"),
    secrets=secrets,
) as llms:
    reply = await llms.ask("Привет")
```

KV v2, путь `llmbroker/{ref}`. Точка монтирования KV по умолчанию `"secret"` и
настраивается через `mount_point=`.

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

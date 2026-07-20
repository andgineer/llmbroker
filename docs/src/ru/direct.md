# Прямые вызовы модели

Пул (`ask`/`chat`) роутит по многим моделям с фейловером. Иногда нужна **одна
конкретная модель**, вызываемая напрямую — платная frontier-модель для задачи на
качество или одна пуловая модель, которую нужно стримить. Это и даёт
`broker.direct(name)`: клиент ровно к этой модели, **без пула и без фейловера**,
а у async-клиента — со **стримингом**.

## В пуле или напрямую

Все модели живут в одном реестре. Флаг `pool` у записи (по умолчанию `true`)
управляет только членством в пуле:

- `pool = true` — часть роутируемого пула; цель фейловера для `ask`/`chat`.
- `pool = false` — остаётся в реестре, но не роутится. Доступна по имени через
  `direct(...)`.

`broker.direct(name)` работает для **любой** записи — пуловой или нет. Платная
модель — это просто `pool = false`: пул на неё не фейловерит, но она остаётся
полноценной моделью, которую можно вызвать напрямую.

## Настройка платной модели

Начните с шаблона:

```bash
llmbroker preset paid >> llms.toml   # добавит пример с `pool = false`
llmbroker env llms.toml >> .env      # добавит строку ключа с подсказкой
```

```toml
[[llms]]
name        = "frontier"
base_url    = "https://api.anthropic.com/v1"   # любой OpenAI-совместимый endpoint
model       = "claude-opus-4-8"
api_key_ref = "ANTHROPIC_API_KEY"
pool        = false                            # вне фейловера; вызывается напрямую
```

`sync` зеркалит в БД только конфиг (`base_url` / `model` / `api_key_ref`) —
**никогда значение ключа**. Ключ читается из переменной окружения или
secrets-бэкенда в момент вызова, поэтому обновление пресета не трогает секрет.

## Стриминг и ask (async)

```python
async with llmbroker.AsyncBroker("llms.toml") as llms:
    await llms.sync("llms.toml")   # зеркалим конфиг в реестр/БД

    client = await llms.direct("frontier")

    # стриминг — async-итератор текстовых дельт
    async for delta in client.stream("Напиши хокку про брокеров"):
        print(delta, end="", flush=True)

    # либо весь ответ сразу
    result = await client.ask("Дай полный текст")
    print(result.text, result.usage)
```

`direct(...)` работает и с **пуловой** моделью — тот же API, но без роутинга:

```python
free = await llms.direct("groq-llama-3.3-70b")
print((await free.ask("одна конкретная модель, без фейловера")).text)
```

## Синхронно

У блокирующего `Broker` тоже есть `direct(...)`, только с `ask()` (стриминг —
async-only):

```python
with llmbroker.Broker("llms.toml") as llms:
    result = llms.direct("frontier").ask("...")
    print(result.text)
```

## Ошибки

Прямые вызовы бросают из одной иерархии под `LLMRequestError`:

- `UnknownModelError` — в реестре нет записи с таким именем.
- `MissingKeyError` — `api_key_ref` модели не задан (платная модель без ключа —
  здесь это ошибка, в отличие от пуловой, которая просто остаётся неактивной).
- `ProviderError` — провайдер вернул ошибку, с `.status` и `.detail`. Ловите
  грубо либо наследников `AuthError` (401/403) и `RateLimitError` (429/503, с
  `.retry_after`) для точечной обработки.
- `LLMTimeoutError` — вызов не уложился в таймаут.

# Прямые вызовы модели

Пул (`ask`/`chat`) роутит по многим моделям с фейловером. Иногда нужна **одна
конкретная модель**, вызываемая напрямую — платная frontier-модель для задачи на
качество или одна пуловая модель, которую нужно стримить. Это и даёт
`broker.direct(name)`: клиент ровно к этой модели, **без пула и без фейловера**,
а у async-клиента — со **стримингом**.

## Два ортогональных флага

Все модели живут в одном реестре. Роль записи задают два независимых флага:

- `pool` (по умолчанию `true`) — членство в пуле. `pool = false` оставляет
  запись в реестре, но вне роутинга; доступна по имени через `direct(...)`.
- `custom` (по умолчанию `false`) — происхождение. Custom-записи ваши, не часть
  курируемого пресета брокера, поэтому `sync` их никогда не удаляет.

Они независимы: custom-модель может быть и в пуле (`pool = true`), и direct-only
(`pool = false`). `broker.direct(name)` работает для **любой** записи.

## Свои модели

Кладите добавляемые вами модели в массив `[[custom]]` — те же поля, что у
`[[llms]]`, тот же парсер, та же таблица реестра, но с флагом `custom`.

Быстрее всего — `add-model`: он выбирает из курируемого каталога платных
провайдеров и сам дописывает `[[custom]]`-блок:

```bash
llmbroker add-model --into llms.toml            # интерактивно: провайдер, затем модель
# либо неинтерактивно:
llmbroker add-model --into llms.toml --provider anthropic --model claude-opus-4-8
```

По умолчанию `pool = false` (direct-only); флаг `--pool` добавит модель в пул.
Затем задайте ключ, который он подскажет (`llmbroker env llms.toml >> .env`).

Либо впишите блок руками:

```toml
[[custom]]
name        = "frontier"
base_url    = "https://api.anthropic.com/v1"   # любой OpenAI-совместимый endpoint
model       = "claude-opus-4-8"
api_key_ref = "ANTHROPIC_API_KEY"
pool        = false                            # direct-only; вызов через direct("frontier")
```

В обоих случаях `llmbroker env llms.toml >> .env` добавит строку ключа с подсказкой.

Файл — единственный источник правды: добавил `[[custom]]`-блок — добавил модель,
убрал — удалил, затем `sync` зеркалит весь файл в БД. `sync` зеркалит только
конфиг (`base_url` / `model` / `api_key_ref`) — **никогда значение ключа**; ключ
читается из переменной окружения или secrets-бэкенда в момент вызова.

## Обновление пула без потери своих моделей

**Не** перезаписывайте файл через `preset freetier > llms.toml` — это сотрёт ваш
`[[custom]]`-блок. Используйте `--merge`:

```bash
llmbroker preset freetier --merge llms.toml   # обновит [[llms]], сохранит [[custom]]
```

`--merge` переписывает записи `[[llms]]` (managed-пресет) и их `[keys]` из
свежего пресета, сохраняя ваши `[[custom]]`-модели и их ключи. Затем — обычный
`sync`.

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

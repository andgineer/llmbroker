# CLI

## env — получить список переменных окружения

```bash
python -m llmbroker env llms.toml
```

Читает файл конфигурации и выводит имена переменных окружения (`api_key_ref`)
которые нужно заполнить:

```
GROQ_API_KEY=
OPENROUTER_API_KEY=
```

Удобно для создания `.env`-файла: `python -m llmbroker env llms.toml > .env`.

## sync — синхронизировать конфиг в SQLite

```bash
python -m llmbroker sync llms.toml --into sqlite:broker.db
python -m llmbroker sync llms.toml --into sqlite:broker.db --policy if_empty
python -m llmbroker sync llms.toml --into sqlite:broker.db --policy add
```

Применяет список LLM из TOML-файла к SQLite-базе.

| Опция `--policy` | Поведение |
|---|---|
| `mirror` (по умолчанию) | DB = источник точно: добавить новые, обновить изменённые, удалить удалённые |
| `if_empty` | заполнить только если DB пуста, иначе ничего |
| `add` | только добавить новые по имени, существующие не трогать |

Типичный сценарий первого запуска:

```bash
python -m llmbroker sync llms.toml --into sqlite:.deploy/broker.db --policy if_empty
```

Обновить список не затирая ручные правки через admin UI:

```bash
python -m llmbroker sync llms.toml --into sqlite:.deploy/broker.db --policy add
```

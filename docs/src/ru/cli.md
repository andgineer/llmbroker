# CLI

## preset — скачать готовый список LLM

```bash
llmbroker preset freetier > llms.toml
```

Доступные пресеты:

- `freetier` — бесплатные endpoints от Groq, OpenRouter и Gemini

## env — сформировать .env с ключами

```bash
llmbroker env llms.toml > .env   # из локального конфига
llmbroker env freetier > .env    # или сразу по имени пресета, без локального файла
```

Печатает заготовку `.env`: над каждым ключом — подсказка, где его получить:

```
# OPENROUTER_API_KEY — Create a free API key at [openrouter](https://openrouter.ai/keys).
OPENROUTER_API_KEY=
```

Аргумент — путь к конфигу, если такой файл есть; иначе имя пресета из каталога,
те же имена, что понимает `preset`.

Сами ключи получите у провайдеров и впишите. Брокер, созданный по пути к файлу
конфига, сам читает соседний `.env`; переменная окружения всегда важнее его.
Ключи вообще не обязаны лежать в `.env` — см. [API-ключи](secrets.md).

## sync — залить пресет в БД

```bash
llmbroker sync llms.toml broker.db
llmbroker sync llms.toml "postgresql://host/db"
```

Полная синхронизация БД с файлом пресета: добавляет, обновляет и удаляет
записи — см. [Серверы и кластеры](server.md#datasource).

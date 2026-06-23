# CLI

## env — получить список переменных окружения

```bash
llmbroker env llms.toml
```

Читает файл конфигурации и выводит имена переменных окружения (`api_key_ref`)
которые нужно заполнить:

```
GROQ_API_KEY=
OPENROUTER_API_KEY=
```

Удобно для создания `.env`-файла: `llmbroker env llms.toml > .env`.

## preset — скачать готовый список LLM

```bash
llmbroker preset freetier > llms.toml
```

Скачивает готовый список LLM и выводит его в stdout. Доступные пресеты:

- `freetier` — бесплатные endpoints от Groq, OpenRouter и других
- `smart-freetier` — тот же пул, модели отсортированы по качеству

После сохранения получите список нужных переменных окружения:

```bash
llmbroker env llms.toml
```

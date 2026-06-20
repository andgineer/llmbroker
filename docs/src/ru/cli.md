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

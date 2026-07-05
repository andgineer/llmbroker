# CLI

## preset — download a ready-made LLM list

```bash
llmbroker preset freetier > llms.toml
```

Available presets:

- `freetier` — free endpoints from Groq, OpenRouter and Gemini

## env — generate a .env with the keys

```bash
llmbroker env llms.toml > .env
```

Prints a `.env` skeleton: above each key, a hint where to get it:

```
# OPENROUTER_API_KEY — Create a free API key at [openrouter](https://openrouter.ai/keys).
OPENROUTER_API_KEY=
```

Get the keys themselves from the providers and fill them in. Keys do not have to
live in `.env` — see [API keys](secrets.md).

## sync — load a preset into a DB

```bash
llmbroker sync llms.toml broker.db
llmbroker sync llms.toml "postgresql://host/db"
```

A full synchronization of the DB with the preset file: adds, updates and deletes
entries — see [Servers & clusters](server.md#datasource).

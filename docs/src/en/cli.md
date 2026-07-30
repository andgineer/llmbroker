# CLI

## preset — download a ready-made LLM list

```bash
llmbroker preset freetier > llms.toml
```

Available presets:

- `freetier` — free endpoints from Groq, OpenRouter and Gemini

## env — generate a .env with the keys

```bash
llmbroker env llms.toml > .env   # from a local config
llmbroker env freetier > .env    # or straight from a preset name, no local file
```

Prints a `.env` skeleton: above each key, a hint where to get it:

```
# OPENROUTER_API_KEY — Create a free API key at [openrouter](https://openrouter.ai/keys).
OPENROUTER_API_KEY=
```

The argument is a config file when one exists at that path, otherwise a preset
name fetched from the catalog — the same names `preset` accepts.

Get the keys themselves from the providers and fill them in. A broker built from
a config file path reads that file's sibling `.env` automatically; an exported
environment variable always wins over it. Keys do not have to live in `.env` at
all — see [API keys](secrets.md).

## sync — load a preset into a DB

```bash
llmbroker sync llms.toml broker.db
llmbroker sync llms.toml "postgresql://host/db"
```

A full synchronization of the DB with the preset file: adds, updates and deletes
entries — see [Servers & clusters](server.md#datasource).

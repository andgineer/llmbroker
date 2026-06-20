# CLI

## env — list required environment variables

```bash
python -m llmbroker env llms.toml
```

Reads the config file and prints the environment variable names (`api_key_ref`)
that need to be set:

```
GROQ_API_KEY=
OPENROUTER_API_KEY=
```

Handy for bootstrapping a `.env` file: `python -m llmbroker env llms.toml > .env`.

## sync — sync a config file into SQLite

```bash
python -m llmbroker sync llms.toml --into sqlite:broker.db
python -m llmbroker sync llms.toml --into sqlite:broker.db --policy if_empty
python -m llmbroker sync llms.toml --into sqlite:broker.db --policy add
```

Applies the LLM list from a TOML file to a SQLite database.

| `--policy` | Behaviour |
|---|---|
| `mirror` (default) | DB = source exactly: add new, update changed, remove dropped |
| `if_empty` | fill only if DB is empty, otherwise no-op |
| `add` | only add entries not already present by name |

Typical first-run command:

```bash
python -m llmbroker sync llms.toml --into sqlite:.deploy/broker.db --policy if_empty
```

Update the pool without overwriting admin edits:

```bash
python -m llmbroker sync llms.toml --into sqlite:.deploy/broker.db --policy add
```

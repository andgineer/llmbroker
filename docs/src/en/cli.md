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

# CLI

## env — list required environment variables

```bash
llmbroker env llms.toml
```

Reads the config file and prints the environment variable names (`api_key_ref`)
that need to be set:

```
GROQ_API_KEY=
OPENROUTER_API_KEY=
```

Handy for bootstrapping a `.env` file: `llmbroker env llms.toml > .env`.

## preset — download a curated LLM list

```bash
llmbroker preset freetier > llms.toml
```

Downloads a curated list of LLMs and writes it to stdout. Available presets:

- `freetier` — free-tier endpoints from Groq, OpenRouter, and others
- `smart-freetier` — same pool, models ranked by quality

After saving, generate the required API key variables:

```bash
llmbroker env llms.toml
```

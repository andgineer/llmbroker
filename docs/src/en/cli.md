# CLI

## env — list required environment variables

```bash
llmbroker env llms.toml
```

Reads the config file and prints the environment variable names (`api_key_ref`)
that need to be set. When the config carries a `[keys]` section, each variable is
preceded by a comment (prefixed with the variable name) telling you where to get it:

```
# OPENROUTER_API_KEY — Create a free API key at [openrouter](https://openrouter.ai/keys).
OPENROUTER_API_KEY=
```

These are just the variable names — you still have to get the actual keys from each
provider and fill them in. Saving them as a `.env` file is the quickest start
(`llmbroker env llms.toml > .env`), but llmbroker can read secrets from anywhere:
environment variables, AWS, Vault, or any backend you wire in.

## preset — download a curated LLM list

```bash
llmbroker preset freetier > llms.toml
```

Downloads a curated list of LLMs and writes it to stdout. Available presets:

- `freetier` — free-tier endpoints from Groq, OpenRouter, and Gemini

After saving, list the API keys this pool needs:

```bash
llmbroker env llms.toml
```

Each provider issues its own key (the free-tier ones are free) — sign up, then provide
the keys via a `.env` file or any other secrets backend.

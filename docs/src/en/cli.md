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

## preset --sync — refresh your config from the preset

```bash
llmbroker preset freetier --sync llms.toml
```

Rewrites the preset-managed models and their key hints in `llms.toml`, keeps your
`[[custom]]` entries and their keys, and re-points every alias-following custom
entry at what the paid catalog now recommends. Then it prints a report of what it
did — on every run, no-ops included.

A model the preset dropped is removed only when a replacement you can actually
call arrives; otherwise it stays and the report names the key that would let the
next sync remove it. So refreshing never leaves you with fewer working models.
Keys are read from the environment and from the `.env` next to the target file —
the same pair the broker itself resolves.

A pending key or a kept entry is a normal state and exits 0; only a real failure
(catalog unreachable, a name the merged file would carry twice) exits non-zero.

!!! note "The target is a file — a DB is synced from your code"
    `--sync broker.db` (or a `postgresql://` URL) is refused on purpose. A CLI
    taking a DSN would duplicate connection config your application already owns,
    and would need DB credentials in the CLI's environment — which an app that
    fetches its DSN from Vault cannot provide. Mirror into a registry from your
    own entrypoint instead: see
    [Servers & clusters](server.md#sync).

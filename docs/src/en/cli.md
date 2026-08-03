# CLI

You need none of this to use llmbroker — `Broker()` fetches the pool itself. The
commands are here for the key skeleton, and for a config file you want under
version control.

Every command works offline: a copy of both curated lists ships inside the
package, so a machine with no network falls back to it rather than failing. That
copy is frozen at the llmbroker release you installed, so when it is used the
command says so on stderr — a file you keep should not be silently older than the
curated list.

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

This is the **reviewable** path, not the only one: a broker keeps the same file
current by itself (see [Basic usage](usage.md#syncing)). The command is for when
you want the change in a diff you approve — so it always fetches and always prints
the report, whatever a broker on this host may have checked a minute ago. It does
tell that broker it looked, so the two share one clock instead of each fetching
on its own schedule. A run that finds no news leaves the file byte-identical.

A model whose provider the preset dropped is removed only when you have no key
for that provider, or when the journal next to your config proves the model dead
(a 401/403/404 with no success). Otherwise it stays and keeps routing. So
refreshing never leaves you with fewer working models.

`--sync` takes a preset name and a **file** target. A database target and a file
source are both refused, each for its own reason:

- a DSN would duplicate connection config your application already owns, and
  would need DB credentials in the CLI's environment — which an app that fetches
  its DSN from Vault cannot provide;
- rendering an arbitrary source into a live `.toml` duplicates its `[[custom]]`
  blocks and leaves a file the broker cannot parse.

Mirror into a registry from your own entrypoint instead: see
[Servers & clusters](server.md#sync).

**What the CLI can see.** Keys come from the environment and from the `.env` next
to the target file — the same pair a file-configured broker resolves — and the
death evidence from the `store/` directory next to it, when one exists. A config
whose keys live in Vault, AWS or a database is therefore refreshed by
`broker.sync("freetier")` from the application instead: only the application can
see those keys, and a CLI that cannot see them would keep every entry.

A pending key or a kept entry is a normal state and exits 0; only a real failure
(catalog unreachable, a name the merged file would carry twice, a target
directory that does not exist) exits non-zero.

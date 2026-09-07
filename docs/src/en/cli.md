# CLI

The CLI is not required for normal use: `Broker()` loads the model list itself.
The two commands help you prepare provider API keys and inspect the maintained
model lists.

There is no separate command for updating a model list. A local list
[updates automatically](usage.md#sync), while a database registry should be
updated from your application with `broker.sync("freetier")`. See
[Servers & clusters](server.md#sync).

Both commands first try to load current data from the catalog, with a 10-second
timeout. If that fails, they use the local cached copy and then the copy bundled
with the installed package. This allows the commands to work offline, including
in CI. When the bundled copy is used, the command writes a warning to standard
error; that copy reflects the installed llmbroker version.

## `env`: create a `.env` key template {#env}

```bash
llmbroker env freetier > .env
```

The command prints a `.env` template for the selected model list. Each variable
is preceded by a link for obtaining the key:

```
# OPENROUTER_API_KEY — Create a free API key at [openrouter](https://openrouter.ai/keys).
OPENROUTER_API_KEY=
```

The list name is the command's only argument. Its behavior does not depend on
whether a local registry exists or the broker uses a database. The available
list is:

- `freetier` — free API endpoints from Groq, OpenRouter, Gemini, and Z.AI.

Obtain the keys from the providers and add them to the file. The broker reads
`.env` from the working directory automatically. An environment variable takes
precedence over the value in the file. See [API keys](secrets.md) for other
storage options.

## `list`: show available models {#list}

```bash
llmbroker list
```

The command makes no changes and prints one model per line. A `pool` line
describes a model in the shared pool. A `direct` line describes a paid model that
can be called directly. The remaining fields are its stable alias, provider ID,
model ID, `base_url`, and `api_key_ref`.

```
pool groq-gpt-oss-120b openai/gpt-oss-120b https://api.groq.com/openai/v1 GROQ_API_KEY
direct opus anthropic claude-opus-5 https://api.anthropic.com/v1 ANTHROPIC_API_KEY
```

To use an alias, pass it when creating the broker:
`Broker(direct=["opus"])`. Then call the model with `broker.direct("opus")`.
See [Direct model calls](direct.md).

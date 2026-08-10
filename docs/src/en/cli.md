# CLI

You need none of this to use llmbroker — `Broker()` fetches the pool itself. The
two commands cover the two things llmbroker cannot do for you: get you the
provider keys, and show you which models the curated lists carry.

Both work offline: a copy of both curated lists ships inside the package, so a
machine with no network falls back to it rather than failing. That copy is frozen
at the llmbroker release you installed, so when it is used the command says so on
stderr.

## env — generate a .env with the keys

```bash
llmbroker env freetier > .env
```

Prints a `.env` skeleton for the named curated model list: above each key, a hint
where to get it:

```
# OPENROUTER_API_KEY — Create a free API key at [openrouter](https://openrouter.ai/keys).
OPENROUTER_API_KEY=
```

The preset name is the whole argument, so the command works the same before you
have anything local and on a broker whose registry lives in a database. The
presets you can name:

- `freetier` — free endpoints from Groq, OpenRouter and Gemini

Get the keys themselves from the providers and fill them in. A broker reads the
`.env` in its working directory automatically; an exported environment variable
always wins over it. Keys do not have to live in `.env` at all — see
[API keys](secrets.md).

## list — show what the curated lists carry

```bash
llmbroker list
```

One model per line and nothing written. A `pool` line is a model the pool routes
over anonymously. A `direct` line is a paid model you can reach by name: the
alias comes first, then the provider id, model id, `base_url` and `api_key_ref`.

```
pool groq-gpt-oss-120b openai/gpt-oss-120b https://api.groq.com/openai/v1 GROQ_API_KEY
direct opus anthropic claude-opus-5 https://api.anthropic.com/v1 ANTHROPIC_API_KEY
```

Declare the alias where you build the broker — `Broker(direct=["opus"])` — and
call it with `broker.direct("opus")`. See [Direct model calls](direct.md).

## What the CLI does not do

There is no command that writes a model list, and none that refreshes one.
A model you reach by name is declared in your own code, never stored. A broker keeps its own lineup
current by itself (see [Basic usage](usage.md#sync)), and a broker on a
database registry is refreshed by its own entrypoint calling
`broker.sync("freetier")` — see [Servers & clusters](server.md#sync). That keeps
the connection config and its secrets in one place, and it keeps the merge where
it can see the keys your application will actually resolve: a merge that cannot
see them would keep every entry.

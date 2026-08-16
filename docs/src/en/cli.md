# CLI

You need none of this to use llmbroker — `Broker()` fetches the pool itself. The
two commands cover the two things llmbroker cannot do for you: get you the
provider keys, and show you which models the curated lists carry.

There is no command that writes or refreshes a model list: a broker keeps its own
list current [by itself](usage.md#sync), and a database registry is refreshed by
your application's entrypoint calling `broker.sync("freetier")` — see
[Servers & clusters](server.md#sync).

Both ask the catalog for a fresh list first (a 10-second timeout), then the copy
already fetched onto this machine, and only then the copy shipped inside the
package. So offline and in a network-less CI they answer with what there is rather
than failing; the bundled copy is frozen at the llmbroker release you installed,
and when it comes to that the command warns about it on stderr.

## env — generate a .env with the keys {#env}

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

- `freetier` — free endpoints from Groq, OpenRouter, Gemini and Z.AI

Get the keys themselves from the providers and fill them in. A broker reads the
`.env` in its working directory automatically; an exported environment variable
always wins over it. Keys do not have to live in `.env` at all — see
[API keys](secrets.md).

## list — show what the curated lists carry {#list}

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

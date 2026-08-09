# CLI

You need none of this to use llmbroker — `Broker()` fetches the pool itself. The
two commands cover the two things llmbroker cannot do for you: get you the
provider keys, and pick the paid model you want to reach by name.

Both work offline: a copy of both curated lists ships inside the package, so a
machine with no network falls back to it rather than failing. That copy is frozen
at the llmbroker release you installed, so when it is used the command says so on
stderr.

## env — generate a .env with the keys

```bash
llmbroker env > .env              # the keys this installation's own lineup needs
llmbroker env freetier > .env     # or straight from a preset name, before there is one
```

Prints a `.env` skeleton: above each key, a hint where to get it:

```
# OPENROUTER_API_KEY — Create a free API key at [openrouter](https://openrouter.ai/keys).
OPENROUTER_API_KEY=
```

With no argument it reads the lineup in llmbroker's own directory — the pool your
broker already follows, plus anything `add-model` put beside it. That is the
everyday form.

Name a preset instead when there is no local lineup yet, or when your broker
keeps its registry in a database and there is no local lineup at all:

- `freetier` — free endpoints from Groq, OpenRouter and Gemini

Get the keys themselves from the providers and fill them in. A broker reads the
`.env` in its working directory automatically; an exported environment variable
always wins over it. Keys do not have to live in `.env` at all — see
[API keys](secrets.md).

## add-model — add a paid model of your own

```bash
llmbroker add-model                                          # interactive
llmbroker add-model --provider anthropic --model claude-opus-5
```

Appends the model to this installation's lineup as your own entry, reachable with
`broker.direct(...)` and never routed by the pool. See
[Your own models](direct.md).

## What the CLI does not do

There is no command that refreshes a lineup. A broker keeps its own lineup
current by itself (see [Basic usage](usage.md#sync)), and a broker on a
database registry is refreshed by its own entrypoint calling
`broker.sync("freetier")` — see [Servers & clusters](server.md#sync). That keeps
the connection config and its secrets in one place, and it keeps the merge where
it can see the keys your application will actually resolve: a merge that cannot
see them would keep every entry.

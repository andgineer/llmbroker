[![Build Status](https://github.com/andgineer/llmbroker/workflows/CI/badge.svg)](https://github.com/andgineer/llmbroker/actions)
[![Coverage](https://raw.githubusercontent.com/andgineer/llmbroker/python-coverage-comment-action-data/badge.svg)](https://htmlpreview.github.io/?https://github.com/andgineer/llmbroker/blob/python-coverage-comment-action-data/htmlcov/index.html)
# llmbroker

Turn a crowd of free, rate-limited LLMs into one reliable model — no premium subscription, no single point of failure.

Many free LLMs are unreliable and mediocre on their own. llmbroker pools them and routes
across the pool, turning quantity into dependable, good-enough quality — without paying
for a premium model.

No LangChain, no heavy deps.

```python
llms = llmbroker.Broker("llms.toml")
print(llms.ask("Explain Python decorators in one sentence").text)
```

Put your LLMs in `llms.toml` — or grab a preset:

```bash
llmbroker preset freetier > llms.toml
```

The only setup is the API keys. Ask llmbroker which ones your pool needs — each with a note on
where to get it, ordered easiest-and-most-valuable first:

```bash
llmbroker env llms.toml > .env
```

Running with only some of the keys is the normal, intended mode — add the keys that are easy
and worth it, and the pool assembles itself from whatever is present. A missing key just means
that one model stays inactive; it is not an error.

The same `Broker` scales straight to a server or cluster — add Redis or Postgres to share cooldown state across instances, the calling code stays the same.

**Why llmbroker:**

- **Automatic failover** — when one LLM is rate-limited or down, the next one in
  the pool answers instead; you get a reply, not an error, as long as any LLM is up.
- **Chat, tools & agents** — one-shot `ask`, multi-turn `chat`, and function/tool
  calling for agentic workflows.
- **Async-first** — `llmbroker.AsyncBroker` built on asyncio for FastAPI, agents, and async workers;
  `llmbroker.Broker` wraps the same engine in a blocking API for plain scripts.
- **Pluggable backends** — swap registry, state store, and telemetry independently: DB, Redis etc.
- **API keys configurable storage** — environment variables, DB, AWS, Vault, your own store.
- **Multi-user mode** -- optionally separate secrets and LLMs list for each application user.

# Documentation

[llmbroker](https://andgineer.github.io/llmbroker/)

# Developers

Do not forget to run `. ./activate.sh`.

For work it need [uv](https://github.com/astral-sh/uv) installed.

Use [pre-commit](https://pre-commit.com/#install) hooks for code quality:

    pre-commit install

## Allure test report

* [Allure report](https://andgineer.github.io/llmbroker/builds/tests/)

# Scripts
Install [invoke](https://docs.pyinvoke.org/en/stable/) preferably with [uv tool](https://docs.astral.sh/uv/):

    uv tool install invoke

For a list of available scripts run:

    invoke --list

For more information about a script run:

    invoke <script> --help

## Coverage report
* [Codecov](https://app.codecov.io/gh/andgineer/llmbroker/tree/main/src%2Fllmbroker)
* [Coveralls](https://coveralls.io/github/andgineer/llmbroker)

> Created with cookiecutter using [template](https://github.com/andgineer/cookiecutter-python-package)

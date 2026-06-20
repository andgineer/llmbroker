[![Build Status](https://github.com/andgineer/llmbroker/workflows/CI/badge.svg)](https://github.com/andgineer/llmbroker/actions)
[![Coverage](https://raw.githubusercontent.com/andgineer/llmbroker/python-coverage-comment-action-data/badge.svg)](https://htmlpreview.github.io/?https://github.com/andgineer/llmbroker/blob/python-coverage-comment-action-data/htmlcov/index.html)
# llmbroker

Route LLM calls over a **pool of free LLMs** with automatic round-robin and
cooldown.

No LangChain, no heavy deps.

```python
import llmbroker

llms = llmbroker.Broker(registry=llmbroker.Registry("llms.toml"))
print(llms.ask("Summarize this receipt").text)
```

`llms.toml` is a plain list of `[[llms]]` entries (base\_url, model, api\_key\_ref).
Grab the [freetier preset](presets/freetier.toml) from this repo to start with a
maintained list of free LLM endpoints.

**Why llmbroker:**

- **Round-robin with automatic failover** — 429/503 cools an endpoint and tries
  the next; the caller never sees a rate-limit error unless *every* endpoint fails.
- **No heavy deps** — stdlib-only core; optional backends (`sqlite`, `redis`,
  `postgres`) are submodules you import only when you need them.
- **Dead-simple to start** — a TOML file and env vars, one constructor line, done.
- **Fully customisable** — swap registry, secrets, and telemetry backends
  independently: any DB, any secrets manager (AWS, Vault, …), any storage.
- **Cluster-ready** — add `shared_state=llmbroker.redis.SharedState(...)` to sync
  cooldown state across instances; omit for single-process.
- **Sync and async** — `llmbroker.Broker` for scripts; `llmbroker.AsyncBroker` for
  FastAPI, agents, and async workers.

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

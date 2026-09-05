[![Build Status](https://github.com/andgineer/llmbroker/workflows/CI/badge.svg)](https://github.com/andgineer/llmbroker/actions)
[![Coverage](https://raw.githubusercontent.com/andgineer/llmbroker/python-coverage-comment-action-data/badge.svg)](https://htmlpreview.github.io/?https://github.com/andgineer/llmbroker/blob/python-coverage-comment-action-data/htmlcov/index.html)
# llmbroker

Turn a crowd of free, rate-limited LLMs into one reliable model — no premium
subscription, no single point of failure. No LangChain, no heavy deps.

```bash
pip install llmbroker
llmbroker env freetier > .env   # which API keys to get, and where
```

```python
broker = llmbroker.Broker()     # no config file: the curated pool of free models
reply = broker.ask("Explain decorators in one sentence")
print(reply.text)   # groq rate-limited? gemini answers instead
```

Fill in whichever keys are easy — models without keys just stay inactive.

**Why another router?** LiteLLM or OpenRouter forward your request and hand back the error;
llmbroker *runs* the pool for you: backs off on rate limits and retries with the next model
inside the same call, disables dead keys on its own, and learns which models are weak at which
tasks. Set it up once, never administer it.

| | |
|---|---|
| **Fast, resilient answers** | Automatic failover by default; `fastest_of=2` races models when latency matters |
| **Chat, tools & agents** | `broker.chat(messages, tools=...)`, `run_tool_loop(...)` |
| **Async & streaming** | `AsyncBroker` — same engine for FastAPI / agents / workers, token by token |
| **A paid model by name** | `Broker(direct=["opus"])` — an eternal alias, called past the pool |
| **Scale out** | `Broker("postgresql://…")` — sqlite / Postgres / MongoDB, calling code unchanged |
| **Self-regulating pool** | `reply.record_quality(0.3)` — weak models sink per task kind; rate later with `broker.record_quality(...)` |
| **Nothing hidden** | `broker.snapshot()`, the call journal, tracing by your own `trace_id` |
| **Pluggable secrets** | env vars, DB, AWS, Vault, or your own backend |
| **Multi-user mode** | per-user API keys on top of one shared pool |

[Documentation](https://andgineer.github.io/llmbroker/)

<details>
<summary>Development</summary>

Do not forget to run `. ./activate.sh`. It needs [uv](https://github.com/astral-sh/uv) installed.

Use [pre-commit](https://pre-commit.com/#install) hooks for code quality:

    pre-commit install

Install [invoke](https://docs.pyinvoke.org/en/stable/) preferably with [uv tool](https://docs.astral.sh/uv/):

    uv tool install invoke

For a list of available scripts run `invoke --list`; for details on one, `invoke <script> --help`.

The bundled `freetier` preset drifts as providers change their free tiers; refresh
it with `invoke catalog-refresh`, which prints the maintenance runbook
(`src/llmbroker/presets/freetier-refresh-prompt.md`).

Reports:

* [Allure test report](https://andgineer.github.io/llmbroker/builds/tests/)
* [Codecov](https://app.codecov.io/gh/andgineer/llmbroker/tree/main/src%2Fllmbroker)
* [Coveralls](https://coveralls.io/github/andgineer/llmbroker)

> Created with cookiecutter using [template](https://github.com/andgineer/cookiecutter-python-package)

</details>

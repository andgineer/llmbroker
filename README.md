[![Build Status](https://github.com/andgineer/llmbroker/workflows/CI/badge.svg)](https://github.com/andgineer/llmbroker/actions)
[![Coverage](https://raw.githubusercontent.com/andgineer/llmbroker/python-coverage-comment-action-data/badge.svg)](https://htmlpreview.github.io/?https://github.com/andgineer/llmbroker/blob/python-coverage-comment-action-data/htmlcov/index.html)
# llmbroker

Turn a crowd of free, rate-limited LLMs into one reliable model — no premium
subscription, no single point of failure. No LangChain, no heavy deps.

```bash
pip install llmbroker
llmbroker preset freetier > llms.toml   # ready-made pool of free models
llmbroker env llms.toml > .env          # which API keys to get, and where
```

```python
llms = llmbroker.Broker("llms.toml")
reply = llms.ask("Explain decorators in one sentence")
print(reply.text)   # groq rate-limited? gemini answers instead
```

Fill in whichever keys are easy — models without keys just stay inactive.

| | |
|---|---|
| **Automatic failover** | `llms.ask(...)` — next model answers when one is down |
| **Chat, tools & agents** | `llms.chat(messages, tools=...)`, `run_tool_loop(...)` |
| **Async-first** | `AsyncBroker` — same engine, for FastAPI / agents / workers |
| **Scale out** | `Broker("postgresql://…")` — sqlite / Postgres / MongoDB, calling code unchanged |
| **Self-regulating pool** | `reply.record_quality(0.3)` — weak models sink per task kind |
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
(`presets/freetier-refresh-prompt.md`).

Reports:

* [Allure test report](https://andgineer.github.io/llmbroker/builds/tests/)
* [Codecov](https://app.codecov.io/gh/andgineer/llmbroker/tree/main/src%2Fllmbroker)
* [Coveralls](https://coveralls.io/github/andgineer/llmbroker)

> Created with cookiecutter using [template](https://github.com/andgineer/cookiecutter-python-package)

</details>

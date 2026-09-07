[![Build Status](https://github.com/andgineer/llmbroker/workflows/CI/badge.svg)](https://github.com/andgineer/llmbroker/actions)
[![Coverage](https://raw.githubusercontent.com/andgineer/llmbroker/python-coverage-comment-action-data/badge.svg)](https://htmlpreview.github.io/?https://github.com/andgineer/llmbroker/blob/python-coverage-comment-action-data/htmlcov/index.html)
# llmbroker

Use multiple free LLM providers through one reliable API. llmbroker selects an
available model, recovers from provider errors and rate limits, and learns which
models work best for each task.

Start with no configuration file, paid subscription, or heavy orchestration
framework. Add production storage, per-user keys, or a specific paid model only
when you need them.

```bash
pip install llmbroker
llmbroker env freetier > .env   # required key names and links to obtain them
```

```python
import llmbroker

broker = llmbroker.Broker()
reply = broker.ask("Explain decorators in one sentence")
print(reply.text)
```

Add any provider keys you have; models without keys are skipped automatically.

| | |
|---|---|
| **Resilient by default** | Automatically tries another model after rate limits and provider failures |
| **Lower latency on demand** | `fastest_of=2` queries multiple models and returns the first complete reply |
| **Improves with feedback** | `record_quality()` adapts model selection independently for each operation |
| **Complete application API** | Sync and async calls, streaming, chat, tools, and built-in tool loops |
| **Direct premium access** | `Broker(direct=["opus"])` calls a specific paid model through a stable alias |
| **Ready to scale** | SQLite, PostgreSQL, MongoDB, shared journals, per-user keys, and pluggable secrets |
| **Observable** | Pool state, call history, statistics, `trace_id`, and availability alerts |

When a previously failed model becomes eligible again, llmbroker checks it in
parallel with an available model. Unlike `fastest_of`, this is not a general
race for the quickest reply: it keeps the recovery check from delaying the call
if the failed model is still unavailable. Set `parallel_recovery=False` to make
the check sequential and avoid the extra request.

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

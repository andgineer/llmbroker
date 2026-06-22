# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/andgineer/llmbroker/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                                    |    Stmts |     Miss |   Cover |   Missing |
|---------------------------------------- | -------: | -------: | ------: | --------: |
| src/llmbroker/\_\_about\_\_.py          |        1 |        1 |      0% |         1 |
| src/llmbroker/\_\_main\_\_.py           |        4 |        4 |      0% |       3-8 |
| src/llmbroker/alembic.py                |        2 |        0 |    100% |           |
| src/llmbroker/broker/broker.py          |      100 |       10 |     90% |58, 62, 97, 103, 105-106, 181-182, 196, 204 |
| src/llmbroker/broker/catalog.py         |       84 |        5 |     94% |55-56, 75-77 |
| src/llmbroker/broker/pool.py            |       68 |        2 |     97% |   89, 121 |
| src/llmbroker/broker/pool\_view.py      |       28 |        0 |    100% |           |
| src/llmbroker/broker/result.py          |       42 |        5 |     88% |37, 40-41, 74-75 |
| src/llmbroker/broker/router.py          |       67 |        3 |     96% |145, 172-173 |
| src/llmbroker/broker/state.py           |       24 |        0 |    100% |           |
| src/llmbroker/chat.py                   |       83 |       10 |     88% |112, 162-172 |
| src/llmbroker/cli.py                    |       29 |        0 |    100% |           |
| src/llmbroker/exceptions.py             |        5 |        0 |    100% |           |
| src/llmbroker/models.py                 |       51 |        1 |     98% |       133 |
| src/llmbroker/optimizer.py              |        4 |        0 |    100% |           |
| src/llmbroker/protocols/registry.py     |        5 |        0 |    100% |           |
| src/llmbroker/protocols/secrets.py      |        5 |        0 |    100% |           |
| src/llmbroker/protocols/state\_store.py |        3 |        0 |    100% |           |
| src/llmbroker/protocols/telemetry.py    |        6 |        0 |    100% |           |
| src/llmbroker/sqlite/registry.py        |       49 |        0 |    100% |           |
| src/llmbroker/sqlite/schema.py          |       34 |        0 |    100% |           |
| src/llmbroker/sqlite/secrets.py         |       29 |        0 |    100% |           |
| src/llmbroker/sqlite/state\_store.py    |       44 |        5 |     89% |44-46, 53, 67 |
| src/llmbroker/sqlite/telemetry.py       |       73 |        2 |     97% |   139-140 |
| src/llmbroker/standalone/registry.py    |       30 |        0 |    100% |           |
| src/llmbroker/standalone/secrets.py     |       39 |        3 |     92% |54, 61, 66 |
| src/llmbroker/standalone/telemetry.py   |       33 |        2 |     94% |    23, 34 |
| src/llmbroker/sync.py                   |      106 |       12 |     89% |82, 101, 103, 130-131, 163-164, 212, 215, 218, 221, 226 |
| **TOTAL**                               | **1048** |   **65** | **94%** |           |


## Setup coverage badge

Below are examples of the badges you can use in your main branch `README` file.

### Direct image

[![Coverage badge](https://raw.githubusercontent.com/andgineer/llmbroker/python-coverage-comment-action-data/badge.svg)](https://htmlpreview.github.io/?https://github.com/andgineer/llmbroker/blob/python-coverage-comment-action-data/htmlcov/index.html)

This is the one to use if your repository is private or if you don't want to customize anything.

### [Shields.io](https://shields.io) Json Endpoint

[![Coverage badge](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/andgineer/llmbroker/python-coverage-comment-action-data/endpoint.json)](https://htmlpreview.github.io/?https://github.com/andgineer/llmbroker/blob/python-coverage-comment-action-data/htmlcov/index.html)

Using this one will allow you to [customize](https://shields.io/endpoint) the look of your badge.
It won't work with private repositories. It won't be refreshed more than once per five minutes.

### [Shields.io](https://shields.io) Dynamic Badge

[![Coverage badge](https://img.shields.io/badge/dynamic/json?color=brightgreen&label=coverage&query=%24.message&url=https%3A%2F%2Fraw.githubusercontent.com%2Fandgineer%2Fllmbroker%2Fpython-coverage-comment-action-data%2Fendpoint.json)](https://htmlpreview.github.io/?https://github.com/andgineer/llmbroker/blob/python-coverage-comment-action-data/htmlcov/index.html)

This one will always be the same color. It won't work for private repos. I'm not even sure why we included it.

## What is that?

This branch is part of the
[python-coverage-comment-action](https://github.com/marketplace/actions/python-coverage-comment)
GitHub Action. All the files in this branch are automatically generated and may be
overwritten at any moment.
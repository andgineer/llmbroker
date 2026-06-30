# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/andgineer/llmbroker/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                                    |    Stmts |     Miss |   Cover |   Missing |
|---------------------------------------- | -------: | -------: | ------: | --------: |
| src/llmbroker/\_\_about\_\_.py          |        1 |        1 |      0% |         1 |
| src/llmbroker/\_\_main\_\_.py           |        4 |        4 |      0% |       3-8 |
| src/llmbroker/broker/broker.py          |      154 |       16 |     90% |130, 143, 145-146, 231-232, 269-273, 285-288, 296 |
| src/llmbroker/broker/catalog.py         |       84 |        5 |     94% |55-56, 75-77 |
| src/llmbroker/broker/pool.py            |      137 |        7 |     95% |95, 115-118, 120, 188 |
| src/llmbroker/broker/pool\_view.py      |       28 |        0 |    100% |           |
| src/llmbroker/broker/result.py          |       42 |        4 |     90% |37, 40-41, 76 |
| src/llmbroker/broker/router.py          |       73 |        3 |     96% |154, 181-182 |
| src/llmbroker/broker/state.py           |       32 |        0 |    100% |           |
| src/llmbroker/chat.py                   |       83 |       10 |     88% |112, 162-172 |
| src/llmbroker/cli.py                    |       76 |        1 |     99% |        37 |
| src/llmbroker/exceptions.py             |        5 |        0 |    100% |           |
| src/llmbroker/integrations/alembic.py   |        2 |        0 |    100% |           |
| src/llmbroker/models.py                 |       51 |        1 |     98% |       133 |
| src/llmbroker/mongodb/registry.py       |       43 |        0 |    100% |           |
| src/llmbroker/mongodb/schema.py         |       24 |        1 |     96% |        30 |
| src/llmbroker/mongodb/secrets.py        |       25 |        0 |    100% |           |
| src/llmbroker/mongodb/state\_store.py   |       42 |        5 |     88% |32-34, 41, 55 |
| src/llmbroker/mongodb/telemetry.py      |       53 |        0 |    100% |           |
| src/llmbroker/optimizer.py              |      169 |        8 |     95% |176-178, 181-183, 186, 235 |
| src/llmbroker/postgres/registry.py      |       49 |        0 |    100% |           |
| src/llmbroker/postgres/schema.py        |       19 |        1 |     95% |        90 |
| src/llmbroker/postgres/secrets.py       |       29 |        0 |    100% |           |
| src/llmbroker/postgres/state\_store.py  |       44 |        5 |     89% |37-39, 46, 60 |
| src/llmbroker/postgres/telemetry.py     |       57 |        0 |    100% |           |
| src/llmbroker/protocols/registry.py     |        7 |        0 |    100% |           |
| src/llmbroker/protocols/secrets.py      |        5 |        0 |    100% |           |
| src/llmbroker/protocols/state\_store.py |        3 |        0 |    100% |           |
| src/llmbroker/protocols/telemetry.py    |        6 |        0 |    100% |           |
| src/llmbroker/redis/state\_store.py     |       50 |        5 |     90% |31, 52, 59, 73, 88 |
| src/llmbroker/sqlite/registry.py        |       49 |        0 |    100% |           |
| src/llmbroker/sqlite/schema.py          |       34 |        0 |    100% |           |
| src/llmbroker/sqlite/secrets.py         |       29 |        0 |    100% |           |
| src/llmbroker/sqlite/state\_store.py    |       44 |        5 |     89% |44-46, 53, 67 |
| src/llmbroker/sqlite/telemetry.py       |       73 |        3 |     96% |139-140, 184 |
| src/llmbroker/standalone/registry.py    |       36 |        1 |     97% |        57 |
| src/llmbroker/standalone/secrets.py     |       39 |        3 |     92% |54, 61, 66 |
| src/llmbroker/standalone/telemetry.py   |       33 |        2 |     94% |    23, 34 |
| src/llmbroker/sync.py                   |      106 |       12 |     89% |82, 101, 103, 130-131, 163-164, 212, 215, 218, 221, 226 |
| **TOTAL**                               | **1840** |  **103** | **94%** |           |


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
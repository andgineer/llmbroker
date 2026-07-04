# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/andgineer/llmbroker/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                                      |    Stmts |     Miss |   Cover |   Missing |
|------------------------------------------ | -------: | -------: | ------: | --------: |
| src/llmbroker/\_\_about\_\_.py            |        1 |        1 |      0% |         1 |
| src/llmbroker/\_\_main\_\_.py             |        4 |        4 |      0% |       3-8 |
| src/llmbroker/aws/secrets.py              |       43 |        2 |     95% |    55, 71 |
| src/llmbroker/broker/broker.py            |      315 |       11 |     97% |245, 259-263, 308, 311, 419, 435, 439 |
| src/llmbroker/broker/catalog.py           |      119 |        2 |     98% |     59-60 |
| src/llmbroker/broker/pool.py              |      242 |        1 |     99% |       103 |
| src/llmbroker/broker/pool\_view.py        |       28 |        0 |    100% |           |
| src/llmbroker/broker/result.py            |       42 |        4 |     90% |37, 40-41, 76 |
| src/llmbroker/broker/router.py            |       76 |        3 |     96% |173, 192-193 |
| src/llmbroker/chat.py                     |       95 |       11 |     88% |41, 125, 175-185 |
| src/llmbroker/cli.py                      |       98 |        1 |     99% |        59 |
| src/llmbroker/exceptions.py               |        5 |        0 |    100% |           |
| src/llmbroker/integrations/alembic.py     |        2 |        0 |    100% |           |
| src/llmbroker/models.py                   |      175 |        5 |     97% |129, 145, 238, 241, 452 |
| src/llmbroker/mongodb/registry.py         |       56 |        0 |    100% |           |
| src/llmbroker/mongodb/schema.py           |       25 |        2 |     92% |    20, 30 |
| src/llmbroker/mongodb/secrets.py          |       25 |        0 |    100% |           |
| src/llmbroker/mongodb/state\_store.py     |       52 |        1 |     98% |        43 |
| src/llmbroker/mongodb/telemetry.py        |       53 |        1 |     98% |       136 |
| src/llmbroker/optimizer.py                |      225 |        7 |     97% |181, 324, 327-329, 332, 371 |
| src/llmbroker/postgres/registry.py        |       68 |        0 |    100% |           |
| src/llmbroker/postgres/schema.py          |       33 |        1 |     97% |       117 |
| src/llmbroker/postgres/secrets.py         |       29 |        0 |    100% |           |
| src/llmbroker/postgres/state\_store.py    |       50 |        1 |     98% |        56 |
| src/llmbroker/postgres/telemetry.py       |       57 |        1 |     98% |       150 |
| src/llmbroker/protocols/backend\_stack.py |       11 |        1 |     91% |        32 |
| src/llmbroker/protocols/registry.py       |        7 |        0 |    100% |           |
| src/llmbroker/protocols/secrets.py        |        5 |        0 |    100% |           |
| src/llmbroker/protocols/state\_store.py   |        5 |        0 |    100% |           |
| src/llmbroker/protocols/telemetry.py      |        6 |        0 |    100% |           |
| src/llmbroker/redis/state\_store.py       |       95 |        8 |     92% |50, 89, 135-137, 192-194 |
| src/llmbroker/sqlite/registry.py          |       67 |        0 |    100% |           |
| src/llmbroker/sqlite/schema.py            |       59 |        5 |     92% |162-164, 167-168 |
| src/llmbroker/sqlite/secrets.py           |       29 |        0 |    100% |           |
| src/llmbroker/sqlite/state\_store.py      |       49 |        1 |     98% |        60 |
| src/llmbroker/sqlite/telemetry.py         |       73 |        3 |     96% |139-140, 184 |
| src/llmbroker/standalone/registry.py      |       91 |        6 |     93% |91-94, 135, 149 |
| src/llmbroker/standalone/secrets.py       |       39 |        3 |     92% |54, 61, 66 |
| src/llmbroker/standalone/telemetry.py     |       33 |        2 |     94% |    23, 34 |
| src/llmbroker/sync.py                     |      103 |       10 |     90% |82, 102, 104, 152-153, 201, 210, 213, 216, 221 |
| src/llmbroker/vault/secrets.py            |       30 |        0 |    100% |           |
| **TOTAL**                                 | **2620** |   **98** | **96%** |           |


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
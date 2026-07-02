# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/andgineer/llmbroker/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                                    |    Stmts |     Miss |   Cover |   Missing |
|---------------------------------------- | -------: | -------: | ------: | --------: |
| src/llmbroker/\_\_about\_\_.py          |        1 |        1 |      0% |         1 |
| src/llmbroker/\_\_main\_\_.py           |        4 |        4 |      0% |       3-8 |
| src/llmbroker/aws/secrets.py            |       43 |        2 |     95% |    55, 71 |
| src/llmbroker/broker/broker.py          |      126 |        4 |     97% |68, 127, 213-214 |
| src/llmbroker/broker/catalog.py         |       84 |        5 |     94% |55-56, 75-77 |
| src/llmbroker/broker/pool.py            |      126 |        7 |     94% |98, 118-121, 123, 162 |
| src/llmbroker/broker/pool\_view.py      |       28 |        0 |    100% |           |
| src/llmbroker/broker/result.py          |       42 |        4 |     90% |37, 40-41, 76 |
| src/llmbroker/broker/router.py          |       82 |        5 |     94% |94-95, 188, 207-208 |
| src/llmbroker/broker/state.py           |       24 |        0 |    100% |           |
| src/llmbroker/chat.py                   |       95 |       11 |     88% |41, 125, 175-185 |
| src/llmbroker/cli.py                    |      104 |        1 |     99% |        73 |
| src/llmbroker/exceptions.py             |        5 |        0 |    100% |           |
| src/llmbroker/integrations/alembic.py   |        2 |        0 |    100% |           |
| src/llmbroker/models.py                 |      101 |        1 |     99% |       271 |
| src/llmbroker/mongodb/registry.py       |       45 |        0 |    100% |           |
| src/llmbroker/mongodb/schema.py         |       24 |        2 |     92% |    20, 30 |
| src/llmbroker/mongodb/secrets.py        |       25 |        0 |    100% |           |
| src/llmbroker/mongodb/state\_store.py   |       37 |        1 |     97% |        43 |
| src/llmbroker/mongodb/telemetry.py      |       53 |        1 |     98% |       136 |
| src/llmbroker/optimizer.py              |      132 |        6 |     95% |144, 147-149, 152, 191 |
| src/llmbroker/postgres/registry.py      |       53 |        0 |    100% |           |
| src/llmbroker/postgres/schema.py        |       29 |        1 |     97% |       101 |
| src/llmbroker/postgres/secrets.py       |       29 |        0 |    100% |           |
| src/llmbroker/postgres/state\_store.py  |       29 |        1 |     97% |        37 |
| src/llmbroker/postgres/telemetry.py     |       57 |        1 |     98% |       150 |
| src/llmbroker/protocols/registry.py     |        7 |        0 |    100% |           |
| src/llmbroker/protocols/secrets.py      |        5 |        0 |    100% |           |
| src/llmbroker/protocols/state\_store.py |        3 |        0 |    100% |           |
| src/llmbroker/protocols/telemetry.py    |        6 |        0 |    100% |           |
| src/llmbroker/redis/state\_store.py     |       30 |        2 |     93% |    30, 49 |
| src/llmbroker/sqlite/registry.py        |       53 |        0 |    100% |           |
| src/llmbroker/sqlite/schema.py          |       51 |        5 |     90% |137-139, 142-143 |
| src/llmbroker/sqlite/secrets.py         |       29 |        0 |    100% |           |
| src/llmbroker/sqlite/state\_store.py    |       29 |        1 |     97% |        41 |
| src/llmbroker/sqlite/telemetry.py       |       73 |        3 |     96% |139-140, 184 |
| src/llmbroker/standalone/registry.py    |       57 |        1 |     98% |        96 |
| src/llmbroker/standalone/secrets.py     |       39 |        3 |     92% |54, 61, 66 |
| src/llmbroker/standalone/telemetry.py   |       33 |        2 |     94% |    23, 34 |
| src/llmbroker/sync.py                   |      106 |       12 |     89% |82, 101, 103, 130-131, 163-164, 212, 215, 218, 221, 226 |
| src/llmbroker/vault/secrets.py          |       30 |        0 |    100% |           |
| **TOTAL**                               | **1931** |   **87** | **95%** |           |


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
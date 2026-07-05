# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/andgineer/llmbroker/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                                  |    Stmts |     Miss |   Cover |   Missing |
|-------------------------------------- | -------: | -------: | ------: | --------: |
| src/llmbroker/\_\_about\_\_.py        |        1 |        1 |      0% |         1 |
| src/llmbroker/\_\_main\_\_.py         |        4 |        4 |      0% |       3-8 |
| src/llmbroker/aws/secrets.py          |       32 |        2 |     94% |    43, 54 |
| src/llmbroker/backends/driver.py      |        8 |        0 |    100% |           |
| src/llmbroker/backends/inmemory.py    |       34 |        2 |     94% |    19, 52 |
| src/llmbroker/backends/ports.py       |       88 |        0 |    100% |           |
| src/llmbroker/backends/spec.py        |        7 |        0 |    100% |           |
| src/llmbroker/broker/broker.py        |      143 |        3 |     98% |80, 143, 272 |
| src/llmbroker/broker/catalog.py       |       68 |        0 |    100% |           |
| src/llmbroker/broker/learning.py      |      118 |        4 |     97% |96, 99, 145, 197 |
| src/llmbroker/broker/pool.py          |      154 |        1 |     99% |        61 |
| src/llmbroker/broker/pool\_view.py    |       29 |        4 |     86% |     34-37 |
| src/llmbroker/broker/result.py        |       42 |        6 |     86% | 43, 81-85 |
| src/llmbroker/broker/router.py        |       79 |        3 |     96% |184, 204-205 |
| src/llmbroker/broker/source.py        |       38 |        2 |     95% |     39-40 |
| src/llmbroker/chat.py                 |       95 |       11 |     88% |41, 125, 175-185 |
| src/llmbroker/cli.py                  |      105 |        4 |     96% |43, 115-117 |
| src/llmbroker/exceptions.py           |        3 |        0 |    100% |           |
| src/llmbroker/integrations/alembic.py |        2 |        0 |    100% |           |
| src/llmbroker/models.py               |       61 |        0 |    100% |           |
| src/llmbroker/mongodb/driver.py       |       84 |        4 |     95% |20, 62, 143-144 |
| src/llmbroker/mongodb/registry.py     |        6 |        0 |    100% |           |
| src/llmbroker/mongodb/secrets.py      |        6 |        0 |    100% |           |
| src/llmbroker/mongodb/store.py        |        8 |        0 |    100% |           |
| src/llmbroker/optimizer.py            |       66 |        3 |     95% |69, 129, 138 |
| src/llmbroker/postgres/driver.py      |      138 |        5 |     96% |110, 125, 127, 219-220 |
| src/llmbroker/postgres/registry.py    |        6 |        0 |    100% |           |
| src/llmbroker/postgres/secrets.py     |        6 |        0 |    100% |           |
| src/llmbroker/postgres/store.py       |        8 |        0 |    100% |           |
| src/llmbroker/protocols/registry.py   |        8 |        0 |    100% |           |
| src/llmbroker/protocols/secrets.py    |        5 |        0 |    100% |           |
| src/llmbroker/protocols/store.py      |        7 |        0 |    100% |           |
| src/llmbroker/sqlite/driver.py        |      154 |        2 |     99% |   225-226 |
| src/llmbroker/sqlite/registry.py      |        6 |        0 |    100% |           |
| src/llmbroker/sqlite/secrets.py       |        6 |        0 |    100% |           |
| src/llmbroker/sqlite/store.py         |        8 |        0 |    100% |           |
| src/llmbroker/standalone/registry.py  |       49 |        1 |     98% |        81 |
| src/llmbroker/standalone/secrets.py   |       34 |        3 |     91% |46, 53, 58 |
| src/llmbroker/standalone/store.py     |      127 |        3 |     98% |170, 189-190 |
| src/llmbroker/sync.py                 |       93 |        5 |     95% |76, 137-138, 189, 194 |
| src/llmbroker/vault/secrets.py        |       19 |        0 |    100% |           |
| **TOTAL**                             | **1955** |   **73** | **96%** |           |


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
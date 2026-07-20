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
| src/llmbroker/broker/broker.py        |      170 |        3 |     98% |85, 149, 341 |
| src/llmbroker/broker/catalog.py       |       73 |        0 |    100% |           |
| src/llmbroker/broker/learning.py      |      125 |        7 |     94% |60-63, 108, 111, 157 |
| src/llmbroker/broker/pool.py          |      162 |        1 |     99% |        62 |
| src/llmbroker/broker/pool\_view.py    |       22 |        0 |    100% |           |
| src/llmbroker/broker/result.py        |       44 |        0 |    100% |           |
| src/llmbroker/broker/router.py        |      107 |        2 |     98% |   241-242 |
| src/llmbroker/broker/source.py        |       38 |        2 |     95% |     39-40 |
| src/llmbroker/chat.py                 |      130 |       14 |     89% |42, 113-114, 167-168, 198, 260-267 |
| src/llmbroker/cli.py                  |      128 |        6 |     95% |110-112, 153-155 |
| src/llmbroker/direct.py               |      112 |        8 |     93% |102, 163, 167-168, 171, 174, 233-234 |
| src/llmbroker/exceptions.py           |       20 |        0 |    100% |           |
| src/llmbroker/integrations/alembic.py |        2 |        0 |    100% |           |
| src/llmbroker/models.py               |       71 |        0 |    100% |           |
| src/llmbroker/mongodb/driver.py       |       84 |        4 |     95% |20, 62, 143-144 |
| src/llmbroker/mongodb/registry.py     |        6 |        0 |    100% |           |
| src/llmbroker/mongodb/secrets.py      |        6 |        0 |    100% |           |
| src/llmbroker/mongodb/store.py        |        8 |        0 |    100% |           |
| src/llmbroker/optimizer.py            |       66 |        1 |     98% |       129 |
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
| src/llmbroker/standalone/registry.py  |       54 |        1 |     98% |        94 |
| src/llmbroker/standalone/secrets.py   |       34 |        3 |     91% |46, 53, 58 |
| src/llmbroker/standalone/store.py     |      127 |        3 |     98% |174, 193-194 |
| src/llmbroker/sync.py                 |       97 |        2 |     98% |   89, 210 |
| src/llmbroker/vault/secrets.py        |       19 |        0 |    100% |           |
| **TOTAL**                             | **2231** |   **73** | **97%** |           |


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
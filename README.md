# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/andgineer/llmbroker/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                                  |    Stmts |     Miss |   Cover |   Missing |
|-------------------------------------- | -------: | -------: | ------: | --------: |
| src/llmbroker/\_\_about\_\_.py        |        1 |        1 |      0% |         1 |
| src/llmbroker/\_\_main\_\_.py         |        4 |        4 |      0% |       3-8 |
| src/llmbroker/aws/secrets.py          |       32 |        2 |     94% |    43, 54 |
| src/llmbroker/backends/driver.py      |        8 |        0 |    100% |           |
| src/llmbroker/backends/inmemory.py    |       36 |        2 |     94% |    19, 60 |
| src/llmbroker/backends/ports.py       |       96 |        0 |    100% |           |
| src/llmbroker/backends/spec.py        |        7 |        0 |    100% |           |
| src/llmbroker/broker/broker.py        |      177 |        3 |     98% |92, 156, 384 |
| src/llmbroker/broker/catalog.py       |       74 |        0 |    100% |           |
| src/llmbroker/broker/learning.py      |      118 |        7 |     94% |51-54, 113, 116, 162 |
| src/llmbroker/broker/pool.py          |      162 |        1 |     99% |        62 |
| src/llmbroker/broker/pool\_view.py    |       22 |        0 |    100% |           |
| src/llmbroker/broker/result.py        |       44 |        0 |    100% |           |
| src/llmbroker/broker/router.py        |      107 |        2 |     98% |   241-242 |
| src/llmbroker/broker/source.py        |       38 |        2 |     95% |     39-40 |
| src/llmbroker/broker/stats.py         |       17 |        0 |    100% |           |
| src/llmbroker/chat.py                 |      130 |       14 |     89% |42, 113-114, 167-168, 198, 260-267 |
| src/llmbroker/cli.py                  |      240 |       23 |     90% |115-117, 150-152, 154-155, 165-166, 185, 188-189, 192, 217-218, 255-257, 291, 322-324 |
| src/llmbroker/direct.py               |      112 |        8 |     93% |102, 163, 167-168, 171, 174, 233-234 |
| src/llmbroker/exceptions.py           |       27 |        0 |    100% |           |
| src/llmbroker/integrations/alembic.py |        2 |        0 |    100% |           |
| src/llmbroker/models.py               |       87 |        0 |    100% |           |
| src/llmbroker/mongodb/driver.py       |       87 |        4 |     95% |21, 63, 154-155 |
| src/llmbroker/mongodb/registry.py     |        6 |        0 |    100% |           |
| src/llmbroker/mongodb/secrets.py      |        6 |        0 |    100% |           |
| src/llmbroker/mongodb/store.py        |        8 |        0 |    100% |           |
| src/llmbroker/optimizer.py            |       66 |        1 |     98% |       129 |
| src/llmbroker/postgres/driver.py      |      141 |        5 |     96% |113, 128, 130, 230-231 |
| src/llmbroker/postgres/registry.py    |        6 |        0 |    100% |           |
| src/llmbroker/postgres/secrets.py     |        6 |        0 |    100% |           |
| src/llmbroker/postgres/store.py       |        8 |        0 |    100% |           |
| src/llmbroker/protocols/registry.py   |        8 |        0 |    100% |           |
| src/llmbroker/protocols/secrets.py    |        5 |        0 |    100% |           |
| src/llmbroker/protocols/store.py      |        8 |        0 |    100% |           |
| src/llmbroker/sqlite/driver.py        |      159 |        2 |     99% |   243-244 |
| src/llmbroker/sqlite/registry.py      |        6 |        0 |    100% |           |
| src/llmbroker/sqlite/secrets.py       |        6 |        0 |    100% |           |
| src/llmbroker/sqlite/store.py         |        8 |        0 |    100% |           |
| src/llmbroker/standalone/registry.py  |       54 |        1 |     98% |        94 |
| src/llmbroker/standalone/secrets.py   |       34 |        3 |     91% |46, 53, 58 |
| src/llmbroker/standalone/store.py     |      143 |        5 |     97% |185, 206-207, 239-240 |
| src/llmbroker/sync.py                 |      100 |        2 |     98% |   90, 229 |
| src/llmbroker/vault/secrets.py        |       19 |        0 |    100% |           |
| **TOTAL**                             | **2425** |   **92** | **96%** |           |


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
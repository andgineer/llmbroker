# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/andgineer/llmbroker/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                                  |    Stmts |     Miss |   Cover |   Missing |
|-------------------------------------- | -------: | -------: | ------: | --------: |
| src/llmbroker/\_\_about\_\_.py        |        1 |        1 |      0% |         1 |
| src/llmbroker/\_\_main\_\_.py         |        4 |        4 |      0% |       3-8 |
| src/llmbroker/aws/secrets.py          |       32 |        2 |     94% |    43, 54 |
| src/llmbroker/backends/driver.py      |        8 |        0 |    100% |           |
| src/llmbroker/backends/inmemory.py    |       36 |        2 |     94% |    19, 60 |
| src/llmbroker/backends/ports.py       |       98 |        0 |    100% |           |
| src/llmbroker/backends/spec.py        |        7 |        0 |    100% |           |
| src/llmbroker/broker/broker.py        |      318 |        9 |     97% |160, 169, 305, 340, 450, 571-573, 743 |
| src/llmbroker/broker/catalog.py       |      111 |        1 |     99% |       122 |
| src/llmbroker/broker/learning.py      |      121 |        7 |     94% |51-54, 113, 116, 170 |
| src/llmbroker/broker/pool.py          |      190 |        1 |     99% |       148 |
| src/llmbroker/broker/pool\_view.py    |       23 |        0 |    100% |           |
| src/llmbroker/broker/result.py        |       45 |        0 |    100% |           |
| src/llmbroker/broker/router.py        |      243 |       16 |     93% |491, 509, 515-517, 538, 542-544, 580-584, 614-615 |
| src/llmbroker/broker/source.py        |       38 |        2 |     95% |     42-43 |
| src/llmbroker/broker/stamps.py        |       35 |        5 |     86% |36, 42, 51, 56-57 |
| src/llmbroker/broker/stats.py         |       17 |        0 |    100% |           |
| src/llmbroker/broker/upstream.py      |      363 |       18 |     95% |172, 184-185, 203, 206, 575, 581, 583, 629-631, 643, 653, 691, 718-719, 747-748 |
| src/llmbroker/chat.py                 |      155 |        6 |     96% |50, 136-137, 234, 264, 332 |
| src/llmbroker/cli.py                  |      246 |       16 |     93% |210-212, 214-215, 225-226, 244, 247-248, 251, 285-286, 358-360 |
| src/llmbroker/direct.py               |      112 |        8 |     93% |120, 187, 191-192, 195, 198, 257-258 |
| src/llmbroker/exceptions.py           |       43 |        0 |    100% |           |
| src/llmbroker/home.py                 |       58 |        7 |     88% |27-28, 31-32, 34, 44-45 |
| src/llmbroker/integrations/alembic.py |        2 |        0 |    100% |           |
| src/llmbroker/models.py               |      204 |        0 |    100% |           |
| src/llmbroker/mongodb/driver.py       |       87 |        4 |     95% |21, 63, 154-155 |
| src/llmbroker/mongodb/registry.py     |        6 |        0 |    100% |           |
| src/llmbroker/mongodb/secrets.py      |        6 |        0 |    100% |           |
| src/llmbroker/mongodb/store.py        |        8 |        0 |    100% |           |
| src/llmbroker/optimizer.py            |       77 |        1 |     99% |       155 |
| src/llmbroker/postgres/driver.py      |      141 |        5 |     96% |113, 128, 130, 230-231 |
| src/llmbroker/postgres/registry.py    |        6 |        0 |    100% |           |
| src/llmbroker/postgres/secrets.py     |        6 |        0 |    100% |           |
| src/llmbroker/postgres/store.py       |        8 |        0 |    100% |           |
| src/llmbroker/protocols/registry.py   |        8 |        0 |    100% |           |
| src/llmbroker/protocols/secrets.py    |        5 |        0 |    100% |           |
| src/llmbroker/protocols/store.py      |        8 |        0 |    100% |           |
| src/llmbroker/sqlite/driver.py        |      182 |        2 |     99% |   297-298 |
| src/llmbroker/sqlite/registry.py      |        6 |        0 |    100% |           |
| src/llmbroker/sqlite/secrets.py       |        6 |        0 |    100% |           |
| src/llmbroker/sqlite/store.py         |        8 |        0 |    100% |           |
| src/llmbroker/standalone/registry.py  |       76 |        1 |     99% |       139 |
| src/llmbroker/standalone/secrets.py   |       74 |        4 |     95% |32, 112, 119, 124 |
| src/llmbroker/standalone/store.py     |      143 |        5 |     97% |185, 206-207, 239-240 |
| src/llmbroker/sync.py                 |      103 |        1 |     99% |       103 |
| src/llmbroker/vault/secrets.py        |       19 |        0 |    100% |           |
| **TOTAL**                             | **3493** |  **128** | **96%** |           |


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
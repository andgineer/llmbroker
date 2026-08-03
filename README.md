# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/andgineer/llmbroker/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                                  |    Stmts |     Miss |   Cover |   Missing |
|-------------------------------------- | -------: | -------: | ------: | --------: |
| src/llmbroker/\_\_about\_\_.py        |        1 |        1 |      0% |         1 |
| src/llmbroker/\_\_main\_\_.py         |        4 |        4 |      0% |       3-8 |
| src/llmbroker/aws/secrets.py          |       32 |        2 |     94% |    43, 54 |
| src/llmbroker/backends/driver.py      |        8 |        0 |    100% |           |
| src/llmbroker/backends/inmemory.py    |       36 |        1 |     97% |        19 |
| src/llmbroker/backends/ports.py       |       98 |        0 |    100% |           |
| src/llmbroker/backends/spec.py        |        7 |        0 |    100% |           |
| src/llmbroker/broker/broker.py        |      359 |        7 |     98% |190, 195, 436, 734-736, 907 |
| src/llmbroker/broker/catalog.py       |      181 |        4 |     98% |74, 84, 215, 244 |
| src/llmbroker/broker/learning.py      |      126 |        7 |     94% |51-54, 113, 116, 182 |
| src/llmbroker/broker/pool.py          |      190 |        1 |     99% |       148 |
| src/llmbroker/broker/pool\_view.py    |       24 |        0 |    100% |           |
| src/llmbroker/broker/result.py        |       45 |        0 |    100% |           |
| src/llmbroker/broker/router.py        |      243 |       16 |     93% |491, 509, 515-517, 538, 542-544, 580-584, 614-615 |
| src/llmbroker/broker/source.py        |       38 |        2 |     95% |     42-43 |
| src/llmbroker/broker/stamps.py        |       35 |        3 |     91% | 42, 56-57 |
| src/llmbroker/broker/stats.py         |       17 |        0 |    100% |           |
| src/llmbroker/broker/upstream.py      |      435 |       17 |     96% |201-202, 268, 271, 344, 797, 803, 849-851, 863, 873, 911, 938-939, 967-968 |
| src/llmbroker/chat.py                 |      155 |        6 |     96% |50, 136-137, 234, 264, 332 |
| src/llmbroker/cli.py                  |      241 |       16 |     93% |211-213, 215-216, 226-227, 245, 248-249, 252, 286-287, 352-354 |
| src/llmbroker/direct.py               |      112 |        8 |     93% |120, 187, 191-192, 195, 198, 257-258 |
| src/llmbroker/exceptions.py           |       43 |        0 |    100% |           |
| src/llmbroker/home.py                 |       58 |        7 |     88% |27-28, 31-32, 34, 44-45 |
| src/llmbroker/integrations/alembic.py |        2 |        0 |    100% |           |
| src/llmbroker/models.py               |      209 |        0 |    100% |           |
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
| src/llmbroker/standalone/registry.py  |       75 |        1 |     99% |       136 |
| src/llmbroker/standalone/secrets.py   |       74 |        4 |     95% |32, 112, 119, 124 |
| src/llmbroker/standalone/store.py     |      143 |        5 |     97% |185, 206-207, 239-240 |
| src/llmbroker/sync.py                 |      103 |        1 |     99% |       103 |
| src/llmbroker/vault/secrets.py        |       19 |        0 |    100% |           |
| **TOTAL**                             | **3681** |  **125** | **97%** |           |


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
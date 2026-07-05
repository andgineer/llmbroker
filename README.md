# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/andgineer/llmbroker/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                                      |    Stmts |     Miss |   Cover |   Missing |
|------------------------------------------ | -------: | -------: | ------: | --------: |
| src/llmbroker/\_\_about\_\_.py            |        1 |        1 |      0% |         1 |
| src/llmbroker/\_\_main\_\_.py             |        4 |        4 |      0% |       3-8 |
| src/llmbroker/aws/secrets.py              |       43 |        2 |     95% |    55, 71 |
| src/llmbroker/broker/broker.py            |      144 |        3 |     98% |81, 153, 282 |
| src/llmbroker/broker/catalog.py           |       68 |        0 |    100% |           |
| src/llmbroker/broker/learning.py          |      113 |        4 |     96% |74, 77, 123, 189 |
| src/llmbroker/broker/pool.py              |      153 |        1 |     99% |        79 |
| src/llmbroker/broker/pool\_view.py        |       28 |        3 |     89% |     30-32 |
| src/llmbroker/broker/result.py            |       38 |        5 |     87% | 39, 73-76 |
| src/llmbroker/broker/router.py            |       79 |        3 |     96% |184, 204-205 |
| src/llmbroker/chat.py                     |       95 |       11 |     88% |41, 125, 175-185 |
| src/llmbroker/cli.py                      |      108 |       18 |     83% |23-24, 48, 106-126 |
| src/llmbroker/exceptions.py               |        5 |        0 |    100% |           |
| src/llmbroker/integrations/alembic.py     |        2 |        0 |    100% |           |
| src/llmbroker/models.py                   |      120 |        5 |     96% |130, 146, 155, 164, 334 |
| src/llmbroker/mongodb/knowledge.py        |       78 |        6 |     92% |182-184, 187-188, 210 |
| src/llmbroker/mongodb/registry.py         |       27 |        0 |    100% |           |
| src/llmbroker/mongodb/schema.py           |       30 |        2 |     93% |    32, 36 |
| src/llmbroker/mongodb/secrets.py          |       25 |        0 |    100% |           |
| src/llmbroker/mongodb/state\_store.py     |       52 |        2 |     96% |   43, 152 |
| src/llmbroker/optimizer.py                |       61 |        4 |     93% |     67-70 |
| src/llmbroker/postgres/knowledge.py       |       84 |        8 |     90% |203-209, 212-214, 238 |
| src/llmbroker/postgres/registry.py        |       34 |        0 |    100% |           |
| src/llmbroker/postgres/schema.py          |       26 |        1 |     96% |       123 |
| src/llmbroker/postgres/secrets.py         |       29 |        0 |    100% |           |
| src/llmbroker/postgres/state\_store.py    |       50 |        2 |     96% |   56, 154 |
| src/llmbroker/protocols/backend\_stack.py |        5 |        0 |    100% |           |
| src/llmbroker/protocols/knowledge.py      |        9 |        0 |    100% |           |
| src/llmbroker/protocols/registry.py       |        8 |        0 |    100% |           |
| src/llmbroker/protocols/secrets.py        |        5 |        0 |    100% |           |
| src/llmbroker/redis/state\_store.py       |       95 |        9 |     91% |50, 89, 135-137, 192-194, 200 |
| src/llmbroker/sqlite/knowledge.py         |      102 |        2 |     98% |   223-224 |
| src/llmbroker/sqlite/registry.py          |       34 |        0 |    100% |           |
| src/llmbroker/sqlite/schema.py            |       50 |        2 |     96% |   171-172 |
| src/llmbroker/sqlite/secrets.py           |       29 |        0 |    100% |           |
| src/llmbroker/sqlite/state\_store.py      |       49 |        2 |     96% |   60, 160 |
| src/llmbroker/standalone/knowledge.py     |      127 |        4 |     97% |160, 170, 189-190 |
| src/llmbroker/standalone/registry.py      |       49 |        1 |     98% |        81 |
| src/llmbroker/standalone/secrets.py       |       39 |        3 |     92% |54, 61, 66 |
| src/llmbroker/sync.py                     |       91 |        6 |     93% |73, 90, 135-136, 187, 192 |
| src/llmbroker/vault/secrets.py            |       30 |        0 |    100% |           |
| **TOTAL**                                 | **2219** |  **114** | **95%** |           |


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
# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/andgineer/llmbroker/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                                  |    Stmts |     Miss |   Cover |   Missing |
|-------------------------------------- | -------: | -------: | ------: | --------: |
| src/llmbroker/\_\_about\_\_.py        |        1 |        1 |      0% |         1 |
| src/llmbroker/\_\_main\_\_.py         |        4 |        4 |      0% |       3-8 |
| src/llmbroker/aws/secrets.py          |       32 |        2 |     94% |    39, 50 |
| src/llmbroker/backends/driver.py      |        8 |        0 |    100% |           |
| src/llmbroker/backends/inmemory.py    |       36 |        1 |     97% |        19 |
| src/llmbroker/backends/ports.py       |       94 |        0 |    100% |           |
| src/llmbroker/backends/spec.py        |        7 |        0 |    100% |           |
| src/llmbroker/broker/aliases.py       |       91 |        3 |     97% |75, 78, 118 |
| src/llmbroker/broker/broker.py        |      228 |        9 |     96% |76, 84, 209-211, 357-359, 519 |
| src/llmbroker/broker/catalog.py       |      201 |        5 |     98% |76, 86, 129, 249, 275 |
| src/llmbroker/broker/keys.py          |       35 |        0 |    100% |           |
| src/llmbroker/broker/learning.py      |      125 |        1 |     99% |       146 |
| src/llmbroker/broker/lineup\_file.py  |       67 |        1 |     99% |        44 |
| src/llmbroker/broker/merge.py         |      122 |        0 |    100% |           |
| src/llmbroker/broker/pool.py          |      186 |        0 |    100% |           |
| src/llmbroker/broker/pool\_view.py    |       22 |        0 |    100% |           |
| src/llmbroker/broker/presets.py       |       98 |        2 |     98% |   164-165 |
| src/llmbroker/broker/refresher.py     |      135 |        1 |     99% |       149 |
| src/llmbroker/broker/report.py        |       48 |        0 |    100% |           |
| src/llmbroker/broker/result.py        |       49 |        0 |    100% |           |
| src/llmbroker/broker/router.py        |      234 |       10 |     96% |521, 525-526, 563-567, 606-607 |
| src/llmbroker/broker/source.py        |       48 |        2 |     96% |     29-30 |
| src/llmbroker/broker/stamps.py        |       35 |        3 |     91% | 42, 56-57 |
| src/llmbroker/broker/stats.py         |       17 |        0 |    100% |           |
| src/llmbroker/chat.py                 |      160 |        6 |     96% |42, 128-129, 215, 262, 330 |
| src/llmbroker/cli.py                  |      209 |       20 |     90% |43-49, 130-132, 134-135, 145-146, 164, 167-168, 171, 189-194, 210-211, 280-282 |
| src/llmbroker/direct.py               |      101 |        8 |     92% |94, 155, 159-160, 163, 166, 221-222 |
| src/llmbroker/exceptions.py           |       43 |        0 |    100% |           |
| src/llmbroker/home.py                 |       58 |        7 |     88% |24-25, 28-29, 31, 41-42 |
| src/llmbroker/http\_status.py         |       18 |        0 |    100% |           |
| src/llmbroker/integrations/alembic.py |        2 |        0 |    100% |           |
| src/llmbroker/journal\_policy.py      |       17 |        0 |    100% |           |
| src/llmbroker/models.py               |      186 |        0 |    100% |           |
| src/llmbroker/mongodb/driver.py       |       87 |        4 |     95% |21, 63, 154-155 |
| src/llmbroker/mongodb/registry.py     |        6 |        0 |    100% |           |
| src/llmbroker/mongodb/secrets.py      |        6 |        0 |    100% |           |
| src/llmbroker/mongodb/store.py        |        8 |        0 |    100% |           |
| src/llmbroker/optimizer.py            |       77 |        1 |     99% |       150 |
| src/llmbroker/postgres/driver.py      |      141 |        5 |     96% |105, 120, 122, 222-223 |
| src/llmbroker/postgres/registry.py    |        6 |        0 |    100% |           |
| src/llmbroker/postgres/secrets.py     |        6 |        0 |    100% |           |
| src/llmbroker/postgres/store.py       |        8 |        0 |    100% |           |
| src/llmbroker/protocols/registry.py   |        8 |        0 |    100% |           |
| src/llmbroker/protocols/secrets.py    |        5 |        0 |    100% |           |
| src/llmbroker/protocols/store.py      |        8 |        0 |    100% |           |
| src/llmbroker/sqlite/driver.py        |      182 |        2 |     99% |   292-293 |
| src/llmbroker/sqlite/registry.py      |        6 |        0 |    100% |           |
| src/llmbroker/sqlite/secrets.py       |        6 |        0 |    100% |           |
| src/llmbroker/sqlite/store.py         |        8 |        0 |    100% |           |
| src/llmbroker/standalone/registry.py  |       76 |        0 |    100% |           |
| src/llmbroker/standalone/secrets.py   |       74 |        4 |     95% |29, 106, 113, 118 |
| src/llmbroker/standalone/store.py     |      136 |        5 |     96% |148, 169-170, 201-202 |
| src/llmbroker/sync.py                 |      103 |        1 |     99% |        95 |
| src/llmbroker/util/atomic.py          |       17 |        3 |     82% |     29-31 |
| src/llmbroker/vault/secrets.py        |       19 |        0 |    100% |           |
| **TOTAL**                             | **3710** |  **111** | **97%** |           |


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
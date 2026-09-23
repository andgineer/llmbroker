# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/andgineer/llmbroker/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                                      |    Stmts |     Miss |   Cover |   Missing |
|------------------------------------------ | -------: | -------: | ------: | --------: |
| src/llmbroker/\_\_about\_\_.py            |        1 |        1 |      0% |         1 |
| src/llmbroker/\_\_main\_\_.py             |        4 |        4 |      0% |       3-8 |
| src/llmbroker/aws/secrets.py              |       43 |        2 |     95% |    39, 67 |
| src/llmbroker/backends/driver.py          |        8 |        0 |    100% |           |
| src/llmbroker/backends/inmemory.py        |       42 |        1 |     98% |        20 |
| src/llmbroker/backends/ports.py           |      100 |        0 |    100% |           |
| src/llmbroker/backends/spec.py            |       13 |        0 |    100% |           |
| src/llmbroker/broker/aliases.py           |       94 |        2 |     98% |    49, 67 |
| src/llmbroker/broker/broker.py            |      257 |        8 |     97% |78, 257, 303-305, 321, 342, 393 |
| src/llmbroker/broker/catalog.py           |      241 |        5 |     98% |57, 67, 105, 317, 355 |
| src/llmbroker/broker/curated.py           |       57 |        0 |    100% |           |
| src/llmbroker/broker/keyring.py           |       73 |        1 |     99% |        16 |
| src/llmbroker/broker/learning.py          |       67 |        0 |    100% |           |
| src/llmbroker/broker/llms.py              |      106 |        0 |    100% |           |
| src/llmbroker/broker/merge.py             |       65 |        0 |    100% |           |
| src/llmbroker/broker/model\_list\_file.py |       58 |        1 |     98% |        37 |
| src/llmbroker/broker/pool.py              |      205 |        0 |    100% |           |
| src/llmbroker/broker/pool\_view.py        |       25 |        0 |    100% |           |
| src/llmbroker/broker/presets.py           |      108 |        2 |     98% |   193-194 |
| src/llmbroker/broker/refresher.py         |      160 |        0 |    100% |           |
| src/llmbroker/broker/report.py            |       25 |        0 |    100% |           |
| src/llmbroker/broker/result.py            |      138 |        3 |     98% |187, 203, 205 |
| src/llmbroker/broker/route\_state.py      |       51 |        0 |    100% |           |
| src/llmbroker/broker/router.py            |      297 |       28 |     91% |119, 203-204, 208, 304-305, 311-314, 349-358, 364-368, 393, 452-453, 582-583, 721-722 |
| src/llmbroker/broker/source.py            |       48 |        2 |     96% |     29-30 |
| src/llmbroker/broker/stamps.py            |       35 |        3 |     91% | 42, 56-57 |
| src/llmbroker/broker/stats.py             |       15 |        0 |    100% |           |
| src/llmbroker/broker/stream\_owner.py     |      403 |       11 |     97% |81, 83, 92, 113, 336, 352, 489, 498, 500, 533, 553 |
| src/llmbroker/broker/streaming.py         |      244 |       14 |     94% |197-198, 271-281, 327-328 |
| src/llmbroker/broker/verdict.py           |       37 |        0 |    100% |           |
| src/llmbroker/chat.py                     |      167 |        5 |     97% |43, 161-162, 290-291 |
| src/llmbroker/cli.py                      |       75 |        0 |    100% |           |
| src/llmbroker/direct.py                   |      115 |        4 |     97% |84, 151, 155-156 |
| src/llmbroker/exceptions.py               |       50 |        0 |    100% |           |
| src/llmbroker/home.py                     |       60 |        7 |     88% |24-25, 28-29, 31, 41-42 |
| src/llmbroker/http\_status.py             |       15 |        0 |    100% |           |
| src/llmbroker/integrations/alembic.py     |        2 |        0 |    100% |           |
| src/llmbroker/journal\_policy.py          |       18 |        0 |    100% |           |
| src/llmbroker/models.py                   |      192 |        0 |    100% |           |
| src/llmbroker/mongodb/driver.py           |       89 |        4 |     96% |22, 64, 183-184 |
| src/llmbroker/mongodb/registry.py         |        6 |        0 |    100% |           |
| src/llmbroker/mongodb/secrets.py          |        6 |        0 |    100% |           |
| src/llmbroker/mongodb/store.py            |        8 |        0 |    100% |           |
| src/llmbroker/optimizer.py                |       81 |        1 |     99% |       167 |
| src/llmbroker/postgres/driver.py          |      144 |        5 |     97% |106, 121, 123, 230-231 |
| src/llmbroker/postgres/registry.py        |        6 |        0 |    100% |           |
| src/llmbroker/postgres/secrets.py         |        6 |        0 |    100% |           |
| src/llmbroker/postgres/store.py           |        8 |        0 |    100% |           |
| src/llmbroker/protocols/chat.py           |        4 |        0 |    100% |           |
| src/llmbroker/protocols/registry.py       |        8 |        0 |    100% |           |
| src/llmbroker/protocols/secrets.py        |        7 |        0 |    100% |           |
| src/llmbroker/protocols/store.py          |        8 |        0 |    100% |           |
| src/llmbroker/sqlite/driver.py            |      184 |        2 |     99% |   300-301 |
| src/llmbroker/sqlite/registry.py          |        6 |        0 |    100% |           |
| src/llmbroker/sqlite/secrets.py           |        6 |        0 |    100% |           |
| src/llmbroker/sqlite/store.py             |        8 |        0 |    100% |           |
| src/llmbroker/standalone/registry.py      |       72 |        0 |    100% |           |
| src/llmbroker/standalone/secrets.py       |       74 |        4 |     95% |29, 106, 113, 118 |
| src/llmbroker/standalone/store.py         |      147 |        5 |     97% |152, 175-176, 208-209 |
| src/llmbroker/sync.py                     |      216 |        2 |     99% |  151, 200 |
| src/llmbroker/tool\_loop.py               |       48 |        1 |     98% |        33 |
| src/llmbroker/util/atomic.py              |       17 |        3 |     82% |     29-31 |
| src/llmbroker/vault/secrets.py            |       29 |        3 |     90% | 26, 62-63 |
| **TOTAL**                                 | **4902** |  **134** | **97%** |           |


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
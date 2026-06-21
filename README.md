# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/andgineer/llmbroker/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                           |    Stmts |     Miss |   Cover |   Missing |
|------------------------------- | -------: | -------: | ------: | --------: |
| src/llmbroker/\_\_about\_\_.py |        1 |        1 |      0% |         1 |
| src/llmbroker/\_\_main\_\_.py  |        4 |        4 |      0% |       3-8 |
| src/llmbroker/alembic.py       |        2 |        0 |    100% |           |
| src/llmbroker/broker.py        |      297 |       34 |     89% |104, 107-108, 144-145, 169, 173, 195-197, 208, 233, 235-236, 307, 381, 399-417, 424, 463-464, 523-527, 536, 545, 581, 590 |
| src/llmbroker/chat.py          |       72 |       10 |     86% |92, 142-152 |
| src/llmbroker/cli.py           |       29 |       29 |      0% |      6-42 |
| src/llmbroker/models.py        |       48 |        0 |    100% |           |
| src/llmbroker/registry.py      |       34 |        0 |    100% |           |
| src/llmbroker/schema.py        |       34 |        0 |    100% |           |
| src/llmbroker/secrets.py       |       43 |        3 |     93% |70, 77, 82 |
| src/llmbroker/sqlite.py        |      185 |       53 |     71% |29, 59-71, 102, 114, 124-125, 129-154, 206-214, 226-227, 240-253, 266-278, 282-289, 328-330, 337, 351, 368 |
| src/llmbroker/state.py         |       24 |        0 |    100% |           |
| src/llmbroker/state\_store.py  |        3 |        0 |    100% |           |
| src/llmbroker/sync.py          |      104 |       12 |     88% |80, 99, 101, 128-129, 161-162, 210, 213, 216, 219, 224 |
| src/llmbroker/telemetry.py     |       38 |        2 |     95% |    43, 54 |
| **TOTAL**                      |  **918** |  **148** | **84%** |           |


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
# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/andgineer/llmbroker/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                           |    Stmts |     Miss |   Cover |   Missing |
|------------------------------- | -------: | -------: | ------: | --------: |
| src/llmbroker/\_\_about\_\_.py |        1 |        1 |      0% |         1 |
| src/llmbroker/\_\_main\_\_.py  |        4 |        4 |      0% |       3-8 |
| src/llmbroker/alembic.py       |        2 |        0 |    100% |           |
| src/llmbroker/broker.py        |      290 |       33 |     89% |104, 139-140, 163, 167, 188-190, 201, 223, 225-226, 296, 368, 385-402, 409, 420, 444-445, 459, 504-508, 517, 559, 568 |
| src/llmbroker/chat.py          |       72 |       10 |     86% |92, 142-152 |
| src/llmbroker/cli.py           |       29 |       29 |      0% |      6-42 |
| src/llmbroker/models.py        |       47 |        0 |    100% |           |
| src/llmbroker/registry.py      |       34 |        0 |    100% |           |
| src/llmbroker/schema.py        |       19 |        0 |    100% |           |
| src/llmbroker/secrets.py       |       38 |        3 |     92% |59, 66, 71 |
| src/llmbroker/shared\_state.py |        3 |        0 |    100% |           |
| src/llmbroker/sqlite.py        |      116 |       54 |     53% |39-50, 66-67, 78, 92-95, 99-108, 126, 129-155, 158-164, 167-198, 201-211, 214-221, 224 |
| src/llmbroker/state.py         |       24 |        0 |    100% |           |
| src/llmbroker/sync.py          |      104 |       12 |     88% |80, 98, 100, 126-127, 159-160, 208, 211, 214, 217, 222 |
| src/llmbroker/telemetry.py     |       38 |        2 |     95% |    38, 49 |
| **TOTAL**                      |  **821** |  **148** | **82%** |           |


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
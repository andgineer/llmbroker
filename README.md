# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/andgineer/llmbroker/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                           |    Stmts |     Miss |   Cover |   Missing |
|------------------------------- | -------: | -------: | ------: | --------: |
| src/llmbroker/\_\_about\_\_.py |        1 |        1 |      0% |         1 |
| src/llmbroker/\_\_main\_\_.py  |        4 |        4 |      0% |       3-8 |
| src/llmbroker/alembic.py       |        2 |        0 |    100% |           |
| src/llmbroker/broker.py        |      272 |       44 |     84% |103, 131-132, 153, 157, 176, 192, 194-195, 257, 262, 328, 346-364, 371, 379, 403-404, 418, 445-446, 449-453, 467, 472-473, 477-479, 496-497, 505-507, 521, 530 |
| src/llmbroker/chat.py          |       72 |       10 |     86% |92, 142-152 |
| src/llmbroker/cli.py           |       52 |       52 |      0% |      7-76 |
| src/llmbroker/models.py        |       44 |        0 |    100% |           |
| src/llmbroker/registry.py      |       34 |        0 |    100% |           |
| src/llmbroker/schema.py        |       19 |        0 |    100% |           |
| src/llmbroker/secrets.py       |       38 |        3 |     92% |59, 66, 71 |
| src/llmbroker/shared\_state.py |        3 |        0 |    100% |           |
| src/llmbroker/sqlite.py        |      107 |       56 |     48% |38-49, 70, 73-76, 83-86, 90-99, 117, 120-146, 149-155, 158-189, 192-202, 205-212, 215 |
| src/llmbroker/state.py         |       24 |        0 |    100% |           |
| src/llmbroker/sync.py          |      103 |       12 |     88% |59, 98-99, 136, 175, 178, 181, 184, 187, 190, 193, 198 |
| src/llmbroker/telemetry.py     |       36 |        2 |     94% |    37, 48 |
| **TOTAL**                      |  **811** |  **184** | **77%** |           |


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
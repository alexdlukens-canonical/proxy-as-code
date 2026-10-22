# Repository Coverage

[Full report](https://htmlpreview.github.io/?https://github.com/alexdlukens-canonical/proxy-as-code/blob/python-coverage-comment-action-data/htmlcov/index.html)

| Name                                                                                              |    Stmts |     Miss |   Branch |   BrPart |   Cover |   Missing |
|-------------------------------------------------------------------------------------------------- | -------: | -------: | -------: | -------: | ------: | --------: |
| src/charm.py                                                                                      |      522 |       70 |      152 |       25 |     84% |145-\>147, 160-162, 174-175, 225, 228, 240-241, 248-258, 264-267, 280, 283-284, 285-\>287, 288-294, 308-\>314, 333-335, 347-\>346, 412, 418-419, 446, 525, 529, 540-544, 549-551, 558, 773, 775-\>774, 777, 798, 805, 831-836, 840-848, 852-858, 862 |
| src/django\_app/manage.py                                                                         |       16 |       16 |        4 |        0 |      0% |      4-30 |
| src/django\_app/terrasquid/\_\_init\_\_.py                                                        |        0 |        0 |        0 |        0 |    100% |           |
| src/django\_app/terrasquid/api/\_\_init\_\_.py                                                    |        0 |        0 |        0 |        0 |    100% |           |
| src/django\_app/terrasquid/api/admin.py                                                           |       48 |        0 |        2 |        0 |    100% |           |
| src/django\_app/terrasquid/api/management/\_\_init\_\_.py                                         |        0 |        0 |        0 |        0 |    100% |           |
| src/django\_app/terrasquid/api/management/commands/\_\_init\_\_.py                                |        0 |        0 |        0 |        0 |    100% |           |
| src/django\_app/terrasquid/api/management/commands/create\_api\_key.py                            |       13 |        0 |        2 |        0 |    100% |           |
| src/django\_app/terrasquid/api/management/commands/list\_api\_keys.py                             |       12 |        0 |        4 |        0 |    100% |           |
| src/django\_app/terrasquid/api/management/commands/render\_squid\_config.py                       |       86 |        0 |       20 |        0 |    100% |           |
| src/django\_app/terrasquid/api/management/commands/revoke\_api\_key.py                            |       15 |        0 |        0 |        0 |    100% |           |
| src/django\_app/terrasquid/api/management/commands/rotate\_api\_key.py                            |       18 |        0 |        0 |        0 |    100% |           |
| src/django\_app/terrasquid/api/migrations/0001\_initial.py                                        |        8 |        0 |        0 |        0 |    100% |           |
| src/django\_app/terrasquid/api/migrations/0002\_renderedconfighistory.py                          |        4 |        0 |        0 |        0 |    100% |           |
| src/django\_app/terrasquid/api/migrations/0003\_remove\_aclrule\_dst\_group\_and\_more.py         |        4 |        0 |        0 |        0 |    100% |           |
| src/django\_app/terrasquid/api/migrations/0004\_acl\_comments.py                                  |        4 |        0 |        0 |        0 |    100% |           |
| src/django\_app/terrasquid/api/migrations/0005\_destinationgroup\_aclrule\_destination\_groups.py |        5 |        0 |        0 |        0 |    100% |           |
| src/django\_app/terrasquid/api/migrations/0006\_renderedconfighistory\_created\_at\_index.py      |        4 |        0 |        0 |        0 |    100% |           |
| src/django\_app/terrasquid/api/migrations/0007\_seed\_config\_version.py                          |        6 |        0 |        0 |        0 |    100% |           |
| src/django\_app/terrasquid/api/migrations/0008\_destinationconfig\_port\_ranges.py                |        5 |        0 |        0 |        0 |    100% |           |
| src/django\_app/terrasquid/api/migrations/\_\_init\_\_.py                                         |        0 |        0 |        0 |        0 |    100% |           |
| src/django\_app/terrasquid/api/models.py                                                          |      139 |        2 |       14 |        0 |     99% |   76, 107 |
| src/django\_app/terrasquid/api/permissions.py                                                     |       12 |        2 |        2 |        0 |     86% |     17-18 |
| src/django\_app/terrasquid/api/policy.py                                                          |       21 |        0 |        4 |        0 |    100% |           |
| src/django\_app/terrasquid/api/serializers.py                                                     |       84 |        5 |       26 |        4 |     92% |90, 98, 124, 128, 182 |
| src/django\_app/terrasquid/api/squid\_render.py                                                   |      109 |        0 |       32 |        0 |    100% |           |
| src/django\_app/terrasquid/api/urls.py                                                            |        9 |        0 |        0 |        0 |    100% |           |
| src/django\_app/terrasquid/api/validators.py                                                      |       19 |        2 |       14 |        2 |     88% |    13, 21 |
| src/django\_app/terrasquid/api/views.py                                                           |      163 |       37 |       22 |        6 |     76% |58-61, 108-109, 119-120, 133-138, 145, 180-183, 209, 226-229, 240-248, 264-265, 270-274 |
| src/django\_app/terrasquid/apps.py                                                                |        5 |        0 |        0 |        0 |    100% |           |
| src/django\_app/terrasquid/metrics.py                                                             |       68 |       17 |        4 |        2 |     74% |21-23, 28-33, 40-43, 61-63, 67 |
| src/django\_app/terrasquid/settings.py                                                            |       38 |        1 |        4 |        2 |     93% |10-\>16, 14 |
| src/django\_app/terrasquid/telemetry.py                                                           |       25 |        6 |        2 |        1 |     74% |     23-28 |
| src/django\_app/terrasquid/urls.py                                                                |        7 |        1 |        2 |        1 |     78% |        19 |
| src/django\_app/terrasquid/wsgi.py                                                                |        6 |        6 |        0 |        0 |      0% |      3-12 |
| src/squid.py                                                                                      |       24 |        0 |        2 |        0 |    100% |           |
| **TOTAL**                                                                                         | **1499** |  **165** |  **312** |   **43** | **88%** |           |


## Setup coverage badge

Below are examples of the badges you can use in your main branch `README` file.

### Direct image

[![Coverage badge](https://raw.githubusercontent.com/alexdlukens-canonical/proxy-as-code/python-coverage-comment-action-data/badge.svg)](https://htmlpreview.github.io/?https://github.com/alexdlukens-canonical/proxy-as-code/blob/python-coverage-comment-action-data/htmlcov/index.html)

This is the one to use if your repository is private or if you don't want to customize anything.

### [Shields.io](https://shields.io) Json Endpoint

[![Coverage badge](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/alexdlukens-canonical/proxy-as-code/python-coverage-comment-action-data/endpoint.json)](https://htmlpreview.github.io/?https://github.com/alexdlukens-canonical/proxy-as-code/blob/python-coverage-comment-action-data/htmlcov/index.html)

Using this one will allow you to [customize](https://shields.io/endpoint) the look of your badge.
It won't work with private repositories. It won't be refreshed more than once per five minutes.

### [Shields.io](https://shields.io) Dynamic Badge

[![Coverage badge](https://img.shields.io/badge/dynamic/json?color=brightgreen&label=coverage&query=%24.message&url=https%3A%2F%2Fraw.githubusercontent.com%2Falexdlukens-canonical%2Fproxy-as-code%2Fpython-coverage-comment-action-data%2Fendpoint.json)](https://htmlpreview.github.io/?https://github.com/alexdlukens-canonical/proxy-as-code/blob/python-coverage-comment-action-data/htmlcov/index.html)

This one will always be the same color. It won't work for private repos. I'm not even sure why we included it.

## What is that?

This branch is part of the
[python-coverage-comment-action](https://github.com/marketplace/actions/python-coverage-comment)
GitHub Action. All the files in this branch are automatically generated and may be
overwritten at any moment.
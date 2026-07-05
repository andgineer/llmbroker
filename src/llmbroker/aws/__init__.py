"""AWS Secrets Manager backend for llmbroker.

Needs the ``aioboto3`` driver (``llmbroker[aws]``); importing this package is
how a host declares that dependency, so a bare ``import llmbroker`` stays
driver-free.
"""

from llmbroker.aws.secrets import Secrets

__all__ = ["Secrets"]

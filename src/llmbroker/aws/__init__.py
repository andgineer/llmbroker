"""AWS Secrets Manager backend for llmbroker.

Needs the ``aioboto3`` driver (``llmbroker[aws]``).
"""

from llmbroker.aws.secrets import Secrets

__all__ = ["Secrets"]

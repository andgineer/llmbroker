"""AWS Secrets Manager backend. Needs ``aioboto3`` (``llmbroker[aws]``); importing
this package is how a host declares that dependency."""

from llmbroker.aws.secrets import Secrets

__all__ = ["Secrets"]

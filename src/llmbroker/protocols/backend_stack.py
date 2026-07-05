"""BackendStack contract: a bundle of registry/secrets/telemetry built from one
shared connection. Implement this shape (three attributes) to wire your own —
see llmbroker.sqlite.Stack / llmbroker.postgres.Stack / llmbroker.mongodb.Stack
for reference implementations.
"""

from typing import Protocol

from llmbroker.protocols.registry import RegistryProtocol
from llmbroker.protocols.secrets import SecretsProtocol
from llmbroker.protocols.telemetry import TelemetryProtocol


class BackendStack(Protocol):
    registry: RegistryProtocol
    secrets: SecretsProtocol
    telemetry: TelemetryProtocol

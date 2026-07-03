"""BackendStack contract: a bundle of registry/secrets/telemetry/state_store
built from one shared connection. Implement this shape (four attributes) to
wire your own — see llmbroker.sqlite.Stack / llmbroker.postgres.Stack /
llmbroker.mongodb.Stack for reference implementations.
"""

from typing import Final, Protocol

from llmbroker.protocols.registry import RegistryProtocol
from llmbroker.protocols.secrets import SecretsProtocol
from llmbroker.protocols.state_store import StateStoreProtocol
from llmbroker.protocols.telemetry import TelemetryProtocol


class BackendStack(Protocol):
    registry: RegistryProtocol
    secrets: SecretsProtocol
    telemetry: TelemetryProtocol
    state_store: StateStoreProtocol | None


class _UnsetType:
    """Sentinel type for `state_store`'s default — distinguishes "not passed"
    from the already-meaningful `state_store=None` ("explicitly disabled").
    Defined once here so `broker/broker.py` and `sync.py` compare against the
    exact same singleton.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "<unset>"


UNSET: Final = _UnsetType()

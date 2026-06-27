"""MongoDB backend: registry, telemetry, state-store, and secrets.

Needs the ``motor`` driver (``llmbroker[mongodb]``). All collections are
``llmbroker_``-prefixed and owned by ``ensure_schema``.
"""

from llmbroker.mongodb.registry import Registry
from llmbroker.mongodb.secrets import Secrets
from llmbroker.mongodb.state_store import StateStore
from llmbroker.mongodb.telemetry import Telemetry

__all__ = ["Registry", "Secrets", "StateStore", "Telemetry"]

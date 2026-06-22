"""SQLite backend: registry, telemetry, state-store, and secrets over one DB file.

Needs the ``aiosqlite`` driver (``llmbroker[sqlite]``); importing this package is
how a host declares that dependency, so a bare ``import llmbroker`` stays
driver-free. All tables are ``llmbroker_``-prefixed and owned by ``ensure_schema``.
"""

from llmbroker.sqlite.registry import Registry
from llmbroker.sqlite.secrets import Secrets
from llmbroker.sqlite.state_store import StateStore
from llmbroker.sqlite.telemetry import Telemetry

__all__ = ["Registry", "Secrets", "StateStore", "Telemetry"]

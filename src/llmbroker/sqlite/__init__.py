"""SQLite backend: registry, telemetry, state-store, and secrets over one DB file.

Needs the ``aiosqlite`` driver (``llmbroker[sqlite]``); importing this package is
how a host declares that dependency, so a bare ``import llmbroker`` stays
driver-free. All tables are ``llmbroker_``-prefixed and owned by ``ensure_schema``.
"""

from pathlib import Path

from llmbroker.sqlite.registry import Registry
from llmbroker.sqlite.secrets import Secrets
from llmbroker.sqlite.state_store import StateStore
from llmbroker.sqlite.telemetry import Telemetry

__all__ = ["Registry", "Secrets", "Stack", "StateStore", "Telemetry"]


class Stack:
    """One SQLite file backing registry, secrets, telemetry, and state store."""

    def __init__(self, db_path: str | Path, *, require_user_id: bool = False) -> None:
        self.registry = Registry(db_path)
        self.secrets = Secrets(db_path, require_user_id=require_user_id)
        self.telemetry = Telemetry(db_path)
        self.state_store = StateStore(db_path)

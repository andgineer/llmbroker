"""MongoDB backend: registry, telemetry, and secrets.

Needs the ``motor`` driver (``llmbroker[mongodb]``). All collections are
``llmbroker_``-prefixed and owned by ``ensure_schema``. ``StateStore`` is
unused by the broker (shared cooldowns derive from the journal) — it stays
importable as a standalone class.
"""

from motor.motor_asyncio import AsyncIOMotorDatabase

from llmbroker.mongodb.registry import Registry
from llmbroker.mongodb.secrets import Secrets
from llmbroker.mongodb.state_store import StateStore
from llmbroker.mongodb.telemetry import Telemetry

__all__ = ["Registry", "Secrets", "Stack", "StateStore", "Telemetry"]


class Stack:
    """One Mongo database backing registry, secrets, and telemetry."""

    def __init__(self, db: AsyncIOMotorDatabase, *, require_user_id: bool = False) -> None:
        self.registry = Registry(db)
        self.secrets = Secrets(db, require_user_id=require_user_id)
        self.telemetry = Telemetry(db)

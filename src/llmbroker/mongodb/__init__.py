"""MongoDB backend: registry, knowledge store, and secrets.

Needs the ``motor`` driver (``llmbroker[mongodb]``). All collections are
``llmbroker_``-prefixed and owned by ``ensure_schema``. ``StateStore`` is
unused by the broker (shared cooldowns derive from the journal) — it stays
importable as a standalone class.
"""

from motor.motor_asyncio import AsyncIOMotorDatabase

from llmbroker.mongodb.knowledge import Knowledge
from llmbroker.mongodb.registry import Registry
from llmbroker.mongodb.secrets import Secrets
from llmbroker.mongodb.state_store import StateStore

__all__ = ["Knowledge", "Registry", "Secrets", "Stack", "StateStore"]


class Stack:
    """One Mongo database backing registry, secrets, and knowledge store."""

    def __init__(self, db: AsyncIOMotorDatabase, *, require_user_id: bool = False) -> None:
        self.registry = Registry(db)
        self.secrets = Secrets(db, require_user_id=require_user_id)
        self.knowledge = Knowledge(db)

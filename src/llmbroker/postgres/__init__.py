"""Postgres backend: registry, knowledge store, and secrets.

Needs the ``asyncpg`` driver (``llmbroker[postgres]``). All tables are
``llmbroker_``-prefixed and owned by ``ensure_schema``. ``StateStore`` is
unused by the broker (shared cooldowns derive from the journal) — it stays
importable as a standalone class.
"""

import asyncpg

from llmbroker.postgres.knowledge import Knowledge
from llmbroker.postgres.registry import Registry
from llmbroker.postgres.secrets import Secrets
from llmbroker.postgres.state_store import StateStore

__all__ = ["Knowledge", "Registry", "Secrets", "Stack", "StateStore"]


class Stack:
    """One asyncpg pool backing registry, secrets, and knowledge store.

    Build the pool yourself first — pool creation is async, ``Broker.__init__``
    is sync: ``pool = await asyncpg.create_pool(dsn)``.
    """

    def __init__(self, pool: asyncpg.Pool, *, require_user_id: bool = False) -> None:
        self.registry = Registry(pool)
        self.secrets = Secrets(pool, require_user_id=require_user_id)
        self.knowledge = Knowledge(pool)

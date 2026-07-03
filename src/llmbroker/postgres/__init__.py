"""Postgres backend: registry, telemetry, state-store, and secrets.

Needs the ``asyncpg`` driver (``llmbroker[postgres]``). All tables are
``llmbroker_``-prefixed and owned by ``ensure_schema``.
"""

import asyncpg

from llmbroker.postgres.registry import Registry
from llmbroker.postgres.secrets import Secrets
from llmbroker.postgres.state_store import StateStore
from llmbroker.postgres.telemetry import Telemetry

__all__ = ["Registry", "Secrets", "Stack", "StateStore", "Telemetry"]


class Stack:
    """One asyncpg pool backing registry, secrets, telemetry, and state store.

    Build the pool yourself first — pool creation is async, ``Broker.__init__``
    is sync: ``pool = await asyncpg.create_pool(dsn)``.
    """

    def __init__(self, pool: asyncpg.Pool, *, require_user_id: bool = False) -> None:
        self.registry = Registry(pool)
        self.secrets = Secrets(pool, require_user_id=require_user_id)
        self.telemetry = Telemetry(pool)
        self.state_store = StateStore(pool)

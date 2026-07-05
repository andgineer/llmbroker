"""Postgres backend: registry, knowledge store, and secrets.

Needs the ``asyncpg`` driver (``llmbroker[postgres]``). All tables are
``llmbroker_``-prefixed and owned by ``ensure_schema``.
"""

from datetime import timedelta

import asyncpg

from llmbroker.backends.ports import StoreKnowledge, StoreRegistry, StoreSecrets
from llmbroker.postgres.driver import PostgresDriver

__all__ = ["Knowledge", "Registry", "Secrets"]

_DEFAULT_RETENTION = timedelta(days=90)


class Registry(StoreRegistry):
    """Postgres-backed mutable registry over ``llmbroker_registry`` — a pure preset mirror."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        super().__init__(PostgresDriver(pool))


class Secrets(StoreSecrets):
    """Postgres-backed mutable secrets store over ``llmbroker_secrets``."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        super().__init__(PostgresDriver(pool))


class Knowledge(StoreKnowledge):
    """Postgres-backed queryable knowledge store over ``llmbroker_calls`` + the
    ``llmbroker_disabled`` admin verdict map."""

    def __init__(self, pool: asyncpg.Pool, *, retention: timedelta = _DEFAULT_RETENTION) -> None:
        super().__init__(PostgresDriver(pool), retention=retention)

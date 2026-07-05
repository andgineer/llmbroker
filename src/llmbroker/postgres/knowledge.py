"""Postgres-backed queryable knowledge store over ``llmbroker_calls`` + the
``llmbroker_disabled`` admin verdict map."""

from datetime import timedelta

import asyncpg

from llmbroker.backends.ports import StoreKnowledge
from llmbroker.postgres.driver import PostgresDriver

_DEFAULT_RETENTION = timedelta(days=90)


class Knowledge(StoreKnowledge):
    """Postgres-backed queryable knowledge store over ``llmbroker_calls`` + the
    ``llmbroker_disabled`` admin verdict map."""

    def __init__(self, pool: asyncpg.Pool, *, retention: timedelta = _DEFAULT_RETENTION) -> None:
        super().__init__(PostgresDriver(pool), retention=retention)

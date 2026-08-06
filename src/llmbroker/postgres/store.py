"""Postgres-backed queryable store over ``llmbroker_calls`` + the
``llmbroker_disabled`` admin verdict map."""

from datetime import timedelta

import asyncpg

from llmbroker.backends.ports import DriverStore
from llmbroker.journal_policy import RETENTION_DEFAULT
from llmbroker.postgres.driver import PostgresDriver


class Store(DriverStore):
    """Postgres-backed queryable store over ``llmbroker_calls`` + the
    ``llmbroker_disabled`` admin verdict map."""

    def __init__(self, pool: asyncpg.Pool, *, retention: timedelta = RETENTION_DEFAULT) -> None:
        super().__init__(PostgresDriver(pool), retention=retention)

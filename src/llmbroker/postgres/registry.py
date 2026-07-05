"""Postgres-backed mutable registry over ``llmbroker_registry`` — a pure preset mirror."""

import asyncpg

from llmbroker.backends.ports import DriverRegistry
from llmbroker.postgres.driver import PostgresDriver


class Registry(DriverRegistry):
    """Postgres-backed mutable registry over ``llmbroker_registry`` — a pure preset mirror."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        super().__init__(PostgresDriver(pool))

"""Postgres-backed mutable secrets store over ``llmbroker_secrets``."""

import asyncpg

from llmbroker.backends.ports import DriverSecrets
from llmbroker.postgres.driver import PostgresDriver


class Secrets(DriverSecrets):
    """Postgres-backed mutable secrets store over ``llmbroker_secrets``."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        super().__init__(PostgresDriver(pool))

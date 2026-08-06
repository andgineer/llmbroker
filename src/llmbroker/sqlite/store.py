"""SQLite-backed queryable store over ``llmbroker_calls`` + the
``llmbroker_disabled`` admin verdict map."""

from datetime import timedelta
from pathlib import Path

from llmbroker.backends.ports import DriverStore
from llmbroker.journal_policy import RETENTION_DEFAULT
from llmbroker.sqlite.driver import SqliteDriver


class Store(DriverStore):
    """SQLite-backed queryable store over ``llmbroker_calls`` + the
    ``llmbroker_disabled`` admin verdict map."""

    def __init__(self, db_path: str | Path, *, retention: timedelta = RETENTION_DEFAULT) -> None:
        super().__init__(SqliteDriver(db_path), retention=retention)

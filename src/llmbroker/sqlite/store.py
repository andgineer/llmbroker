"""SQLite-backed queryable store over ``llmbroker_calls`` + the
``llmbroker_disabled`` admin verdict map."""

from datetime import timedelta
from pathlib import Path

from llmbroker.backends.ports import DriverStore
from llmbroker.sqlite.driver import SqliteDriver

_DEFAULT_RETENTION = timedelta(days=90)


class Store(DriverStore):
    """SQLite-backed queryable store over ``llmbroker_calls`` + the
    ``llmbroker_disabled`` admin verdict map."""

    def __init__(self, db_path: str | Path, *, retention: timedelta = _DEFAULT_RETENTION) -> None:
        super().__init__(SqliteDriver(db_path), retention=retention)

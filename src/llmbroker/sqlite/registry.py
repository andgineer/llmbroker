"""SQLite-backed mutable registry over ``llmbroker_registry`` — a pure preset mirror."""

from pathlib import Path

from llmbroker.backends.ports import DriverRegistry
from llmbroker.sqlite.driver import SqliteDriver


class Registry(DriverRegistry):
    """SQLite-backed mutable registry over ``llmbroker_registry`` — a pure preset mirror."""

    def __init__(self, db_path: str | Path) -> None:
        super().__init__(SqliteDriver(db_path))

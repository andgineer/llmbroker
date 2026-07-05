"""SQLite-backed mutable secrets store over ``llmbroker_secrets``."""

from pathlib import Path

from llmbroker.backends.ports import StoreSecrets
from llmbroker.sqlite.driver import SqliteDriver


class Secrets(StoreSecrets):
    """SQLite-backed mutable secrets store over ``llmbroker_secrets``."""

    def __init__(self, db_path: str | Path) -> None:
        super().__init__(SqliteDriver(db_path))

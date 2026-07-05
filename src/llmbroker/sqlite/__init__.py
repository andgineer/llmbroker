"""SQLite backend: registry, knowledge store, and secrets over one DB file.

Needs the ``aiosqlite`` driver (``llmbroker[sqlite]``); importing this package is
how a host declares that dependency, so a bare ``import llmbroker`` stays
driver-free. All tables are ``llmbroker_``-prefixed and owned by ``ensure_schema``.
"""

from datetime import timedelta
from pathlib import Path

from llmbroker.backends.ports import StoreKnowledge, StoreRegistry, StoreSecrets
from llmbroker.sqlite.driver import SqliteDriver

__all__ = ["Knowledge", "Registry", "Secrets"]

_DEFAULT_RETENTION = timedelta(days=90)


class Registry(StoreRegistry):
    """SQLite-backed mutable registry over ``llmbroker_registry`` — a pure preset mirror."""

    def __init__(self, db_path: str | Path) -> None:
        super().__init__(SqliteDriver(db_path))


class Secrets(StoreSecrets):
    """SQLite-backed mutable secrets store over ``llmbroker_secrets``."""

    def __init__(self, db_path: str | Path) -> None:
        super().__init__(SqliteDriver(db_path))


class Knowledge(StoreKnowledge):
    """SQLite-backed queryable knowledge store over ``llmbroker_calls`` + the
    ``llmbroker_disabled`` admin verdict map."""

    def __init__(self, db_path: str | Path, *, retention: timedelta = _DEFAULT_RETENTION) -> None:
        super().__init__(SqliteDriver(db_path), retention=retention)

"""The per-DB storage contract: record-shaped, not domain-shaped. A driver method
body is the one statement that is genuinely DB-specific; everything else is
``backends.ports``."""

from datetime import datetime
from typing import Protocol

Row = dict[str, object]
Key = tuple[object, ...]


class Driver(Protocol):
    async def ensure_schema(self) -> None: ...

    # ------------------------------------------------------------------
    # Keyed records (registry, disabled, secrets) — every key column is
    # non-null text, so key matching is plain equality.
    # ------------------------------------------------------------------

    async def fetch(self, table: str) -> list[Row]:
        """All rows, ordered by key columns (registry load order feeds selection priority)."""
        ...

    async def get(self, table: str, key: Key) -> Row | None: ...

    async def upsert(self, table: str, key: Key, row: Row) -> None: ...

    async def delete(self, table: str, key: Key) -> bool: ...

    # ------------------------------------------------------------------
    # Journal ops (llmbroker_calls) — strictly append-only: no update op
    # exists; quality is its own appended record.
    # ------------------------------------------------------------------

    async def append(self, table: str, row: Row) -> None: ...

    async def recent(
        self,
        table: str,
        limit: int,
        match: Row | None = None,
        since: datetime | None = None,
    ) -> list[Row]:
        """Newest-first tail; ``match`` is an optional equality filter and ``since``
        an inclusive UTC bound on ``called_at``. Mongo floors both stored values and
        the bound to whole milliseconds — BSON dates carry no finer precision."""
        ...

    async def purge(self, table: str, before: datetime) -> int:
        """Delete rows older than ``before``; returns the count removed."""
        ...

    async def aclose(self) -> None: ...

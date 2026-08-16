"""The per-DB storage contract: one round-trip per read, and no logic two correct
backends could answer differently — that stays in ``backends.ports``. The journal
fold names its columns here; ``backends.spec`` refuses to load if one is renamed away."""

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
        """All rows, ordered by key columns — a stable order, never a ranking (invariant 3)."""
        ...

    async def get(self, table: str, key: Key) -> Row | None: ...

    async def upsert(self, table: str, key: Key, row: Row) -> None: ...

    async def delete(self, table: str, key: Key) -> bool: ...

    # ------------------------------------------------------------------
    # Journal ops (llmbroker_calls) — strictly append-only: no update op
    # exists; quality is its own appended record.
    # ------------------------------------------------------------------

    async def append(self, table: str, row: Row) -> None: ...

    async def journal_view(
        self,
        limit: int,
        match: Row | None = None,
        since: datetime | None = None,
    ) -> list[Row]:
        """Newest-first call rows, each with a ``score`` key holding its newest rating's
        value or ``None``. ``match`` and ``since`` narrow the call rows only; Mongo
        floors stored values and the bound to whole milliseconds."""
        ...

    async def purge(self, table: str, before: datetime) -> int:
        """Delete rows older than ``before``; returns the count removed."""
        ...

    async def aclose(self) -> None: ...

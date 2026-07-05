"""Trivial dict-based ``Driver`` — a test double, and a dependency-free storage
option for hosts that want the full port surface without a database."""

from datetime import UTC, datetime

from llmbroker.backends.driver import Key, Row

_EPOCH = datetime.min.replace(tzinfo=UTC)


class InMemoryDriver:
    """In-process ``Driver`` implementation. Not persisted, not process-shared."""

    def __init__(self) -> None:
        self._tables: dict[str, dict[Key, Row]] = {}
        self._journals: dict[str, list[Row]] = {}

    async def ensure_schema(self) -> None:
        return

    async def fetch(self, table: str) -> list[Row]:
        return [dict(row) for _, row in sorted(self._tables.get(table, {}).items())]

    async def get(self, table: str, key: Key) -> Row | None:
        row = self._tables.get(table, {}).get(key)
        return dict(row) if row is not None else None

    async def upsert(self, table: str, key: Key, row: Row) -> None:
        self._tables.setdefault(table, {})[key] = dict(row)

    async def delete(self, table: str, key: Key) -> bool:
        return self._tables.get(table, {}).pop(key, None) is not None

    async def append(self, table: str, row: Row) -> None:
        self._journals.setdefault(table, []).append(dict(row))

    async def recent(self, table: str, limit: int, match: Row | None = None) -> list[Row]:
        rows = self._journals.get(table, [])
        if match:
            rows = [r for r in rows if all(r.get(k) == v for k, v in match.items())]
        ordered = sorted(rows, key=lambda r: r.get("called_at") or _EPOCH, reverse=True)
        return [dict(r) for r in ordered[:limit]]

    async def purge(self, table: str, before: datetime) -> int:
        rows = self._journals.get(table, [])
        keep = [r for r in rows if (r.get("called_at") or before) >= before]
        removed = len(rows) - len(keep)
        self._journals[table] = keep
        return removed

    async def aclose(self) -> None:
        return

"""Trivial dict-based ``Driver`` — a test double, and a dependency-free storage
option for hosts that want the full port surface without a database."""

from datetime import UTC, datetime

from llmbroker.backends.driver import Key, Row
from llmbroker.journal_policy import KIND_CALL, KIND_QUALITY

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

    async def journal_view(
        self,
        limit: int,
        match: Row | None = None,
        since: datetime | None = None,
    ) -> list[Row]:
        rows = self._journals.get("calls", [])
        ordered = sorted(rows, key=lambda r: r.get("called_at") or _EPOCH, reverse=True)
        newest_rating: dict[object, float] = {}
        for row in ordered:  # newest-first, so the first rating seen per call wins
            if row.get("kind") == KIND_QUALITY and row.get("quality_score") is not None:
                newest_rating.setdefault(row.get("call_id"), row["quality_score"])  # type: ignore[arg-type]
        calls = [r for r in ordered if r.get("kind") == KIND_CALL]
        if match:
            calls = [r for r in calls if all(r.get(k) == v for k, v in match.items())]
        if since is not None:
            calls = [r for r in calls if (r.get("called_at") or _EPOCH) >= since]
        return [{**r, "score": newest_rating.get(r.get("id"))} for r in calls[:limit]]

    async def purge(self, table: str, before: datetime) -> int:
        rows = self._journals.get(table, [])
        keep = [r for r in rows if (r.get("called_at") or before) >= before]
        removed = len(rows) - len(keep)
        self._journals[table] = keep
        return removed

    async def aclose(self) -> None:
        return

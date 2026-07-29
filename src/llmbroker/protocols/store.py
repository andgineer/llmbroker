"""Store contract: record calls; queryable backends also read the journal.

``DisabledMapProtocol`` is the optional admin-verdict half: a tiny mutable
``name -> disabled`` document a backend may additionally implement.
"""

from datetime import datetime
from typing import Protocol, runtime_checkable

from llmbroker.models import Call


class StoreProtocol(Protocol):
    async def record(self, call: Call) -> None: ...
    async def record_quality(
        self,
        llm_name: str,
        operation: str | None,
        score: float,
        *,
        call_id: str | None = None,
        scope: str | None = None,
    ) -> None: ...


@runtime_checkable
class QueryableStoreProtocol(StoreProtocol, Protocol):
    """``since`` must be timezone-aware and bounds the journal inclusively;
    ``limit`` must be >= 1."""

    async def calls(
        self,
        *,
        limit: int,
        scope: str | None = None,
        since: datetime | None = None,
        kind: str | None = None,
        operation: str | None = None,
    ) -> list[Call]: ...


@runtime_checkable
class DisabledMapProtocol(Protocol):
    """Optional capability: the admin disabled-verdict map (``name -> bool``)."""

    async def get_disabled(self, name: str) -> bool: ...
    async def set_disabled(self, name: str, flag: bool) -> None: ...  # noqa: FBT001
    async def seed_disabled(self, names: list[str]) -> None: ...
    async def disabled_map(self) -> dict[str, bool]: ...

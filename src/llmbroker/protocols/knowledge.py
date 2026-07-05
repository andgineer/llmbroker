"""Knowledge contract: record calls; queryable backends also read the journal.

``DisabledMapProtocol`` is the optional admin-verdict half: a tiny mutable
``name -> disabled`` document a backend may additionally implement.
"""

from datetime import datetime
from typing import Protocol, runtime_checkable

from llmbroker.models import Call, LLMMetrics


class KnowledgeProtocol(Protocol):
    async def record(self, call: Call) -> None: ...
    async def record_quality(
        self,
        llm_name: str,
        operation: str | None,
        score: float,
        *,
        call_id: str | None = None,
    ) -> None: ...


@runtime_checkable
class QueryableKnowledgeProtocol(KnowledgeProtocol, Protocol):
    async def calls(self, *, limit: int, scope: str | None = None) -> list[Call]: ...

    async def metrics(
        self,
        *,
        since: datetime | None = None,
        user_id: int | str | None = None,
    ) -> dict[str, LLMMetrics]:
        """Unused by the broker (metrics are served from the journal-rebuild cache when
        an optimizer is attached); kept for hosts reading a backend directly."""
        ...


@runtime_checkable
class DisabledMapProtocol(Protocol):
    """Optional capability: the admin disabled-verdict map (``name -> bool``)."""

    async def get_disabled(self, name: str) -> bool: ...
    async def set_disabled(self, name: str, flag: bool) -> None: ...  # noqa: FBT001
    async def seed_disabled(self, names: list[str]) -> None: ...
    async def disabled_map(self) -> dict[str, bool]: ...

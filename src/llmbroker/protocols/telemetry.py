"""Telemetry contract: record calls; queryable backends also read metrics/journal."""

from datetime import datetime
from typing import Protocol, runtime_checkable

from llmbroker.models import Call, LLMMetrics


class TelemetryProtocol(Protocol):
    async def record(self, call: Call) -> None: ...
    async def record_quality(self, call_id: str, score: float) -> None: ...


@runtime_checkable
class QueryableTelemetryProtocol(TelemetryProtocol, Protocol):
    async def metrics(
        self,
        *,
        since: datetime | None = None,
        user_id: int | str | None = None,
    ) -> dict[str, LLMMetrics]: ...
    async def calls(self, *, limit: int, user_id: int | str | None = None) -> list[Call]: ...
    async def purge_calls(self, *, before: datetime) -> int: ...

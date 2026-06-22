"""PoolView: read-only views of the broker's current pool state."""

from collections.abc import Mapping
from datetime import datetime

from llmbroker.broker.pool import LLMPool
from llmbroker.broker.result import AsyncLLM
from llmbroker.models import LLMMetrics, LLMSnapshot, LLMState
from llmbroker.protocols.telemetry import QueryableTelemetryProtocol, TelemetryProtocol


class PoolView:
    """Live views over the pool: a single LLM handle, the count, a full snapshot."""

    def __init__(
        self,
        pool: LLMPool,
        telemetry: TelemetryProtocol,
        *,
        user_id: int | str | None,
    ) -> None:
        self._pool = pool
        self._telemetry = telemetry
        self._user_id = user_id

    def get(self, name: str) -> AsyncLLM:
        if name not in self._pool:
            raise KeyError(name)
        return AsyncLLM(
            name,
            self._pool.config(name),
            self._pool,
            self._telemetry,
            user_id=self._user_id,
        )

    def count(self) -> int:
        return len(self._pool)

    async def snapshot(self, *, since: datetime | None = None) -> Mapping[str, LLMSnapshot]:
        metrics_map: dict[str, LLMMetrics] = {}
        if isinstance(self._telemetry, QueryableTelemetryProtocol):
            metrics_map = await self._telemetry.metrics(since=since, user_id=self._user_id)
        stored: dict[str, LLMState] = await self._pool.stored_states()
        result: dict[str, LLMSnapshot] = {}
        for name, cfg in self._pool.configs.items():
            metrics = metrics_map.get(name) if metrics_map else None
            state = stored[name] if name in stored else self._pool.state(name)
            result[name] = LLMSnapshot(config=cfg, state=state, metrics=metrics)
        return result

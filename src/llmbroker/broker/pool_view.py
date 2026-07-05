"""PoolView: read-only views of the broker's current pool state."""

from collections.abc import Mapping

from llmbroker.broker.learning import _LearningHook
from llmbroker.broker.pool import LLMPool
from llmbroker.broker.result import AsyncLLM
from llmbroker.models import LLMMetrics, LLMSnapshot
from llmbroker.protocols.telemetry import QueryableTelemetryProtocol, TelemetryProtocol


class PoolView:
    """Live views over the pool: a single LLM handle, the count, a full snapshot."""

    def __init__(self, pool: LLMPool, telemetry: TelemetryProtocol) -> None:
        self._pool = pool
        self._telemetry = telemetry

    def get(self, name: str) -> AsyncLLM:
        if name not in self._pool:
            raise KeyError(name)
        return AsyncLLM(name, self._pool.config(name), self._pool, self._telemetry)

    def count(self) -> int:
        return len(self._pool)

    async def _metrics_map(self) -> dict[str, LLMMetrics]:
        if isinstance(self._telemetry, _LearningHook):
            return self._telemetry.metrics_cache
        if isinstance(self._telemetry, QueryableTelemetryProtocol):
            return await self._telemetry.metrics()
        return {}

    async def snapshot(self) -> Mapping[str, LLMSnapshot]:
        metrics_map = await self._metrics_map()
        result: dict[str, LLMSnapshot] = {}
        for name, cfg in self._pool.configs.items():
            result[name] = LLMSnapshot(
                config=cfg,
                state=self._pool.state(name),
                metrics=metrics_map.get(name),
            )
        return result

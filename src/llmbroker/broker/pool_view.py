"""PoolView: read-only views of the broker's current pool state."""

from collections.abc import Callable

from llmbroker.broker.pool import LLMPool
from llmbroker.broker.result import AsyncLLM, MetricsSource
from llmbroker.models import LLMSnapshot, PendingKey, PoolHealth, PoolSnapshot


class PoolView:
    """Live views over the pool: a single LLM handle, the count, a full snapshot.

    ``has_key`` says whether the installation holds a key at all, a caller's own
    included: the snapshot describes the pool rather than one request."""

    def __init__(  # noqa: PLR0913 - one read-only view over the parts it reports on
        self,
        pool: LLMPool,
        metrics_source: MetricsSource,
        health: Callable[[], PoolHealth],
        direct_missing_keys: Callable[[], tuple[PendingKey, ...]],
        payable: Callable[[], frozenset[str]],
    ) -> None:
        self._pool = pool
        self._metrics_source = metrics_source
        self._health = health
        self._direct_missing_keys = direct_missing_keys
        self._payable = payable

    def get(self, name: str) -> AsyncLLM:
        if name not in self._pool:
            raise KeyError(name)
        return AsyncLLM(name, self._pool.config(name), self._pool, self._metrics_source)

    def count(self) -> int:
        return len(self._pool)

    async def snapshot(self) -> PoolSnapshot:
        metrics_map = await self._metrics_source()
        payable = self._payable()
        result: dict[str, LLMSnapshot] = {}
        for name, cfg in self._pool.configs.items():
            result[name] = LLMSnapshot(
                config=cfg,
                disabled=self._pool.is_disabled(name),
                has_key=cfg.api_key_ref in payable,
                cooldown_until=self._pool.state(name).cooldown_until,
                demoted_operations=tuple(self._pool.demoted_operations(name)),
                metrics=metrics_map.get(name),
            )
        return PoolSnapshot(result, self._health(), self._direct_missing_keys())

"""Per-call result handle and the live per-LLM view returned by the broker."""

import logging

from llmbroker.broker.learning import (
    _DEFAULT_QUALITY_REBUILD_LIMIT,
    _LearningHook,
    metrics_from_calls,
)
from llmbroker.broker.pool import LLMPool
from llmbroker.models import LLMConfig, LLMMetrics, LLMState, Usage
from llmbroker.protocols.store import QueryableStoreProtocol, StoreProtocol

logger = logging.getLogger("llmbroker.broker")


class AsyncResult:
    """Returned by AsyncBroker.ask()/chat()."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        text: str,
        tool_calls: list[dict] | None,
        usage: Usage | None,
        call_id: str,
        llm_name: str,
        operation: str | None = None,
        store: StoreProtocol,
        pool: LLMPool,
    ) -> None:
        self.text = text
        self.tool_calls = tool_calls
        self.usage = usage
        self._call_id = call_id
        self._llm_name = llm_name
        self._operation = operation
        self._store = store
        self._pool = pool

    async def record_quality(self, score: float) -> None:
        if score == 0.0:
            self._pool.mark_quality_fail(self._llm_name)
        await self._store.record_quality(
            self._llm_name,
            self._operation,
            score,
            call_id=self._call_id,
        )


class AsyncLLM:
    """Handle returned by ``AsyncBroker.get(name)`` — live view into broker internals."""

    def __init__(
        self,
        name: str,
        config: LLMConfig,
        pool: LLMPool,
        store: StoreProtocol,
    ) -> None:
        self._name = name
        self._config = config
        self._pool = pool
        self._store = store

    @property
    def config(self) -> LLMConfig:
        return self._config

    @property
    def disabled(self) -> bool:
        return self._pool.is_disabled(self._name)

    async def state(self) -> LLMState:
        return self._pool.state(self._name)

    async def metrics(self) -> LLMMetrics:
        if isinstance(self._store, _LearningHook):
            return self._store.metrics_cache.get(self._name, LLMMetrics(0, None, None))
        if isinstance(self._store, QueryableStoreProtocol):
            rows = await self._store.calls(limit=_DEFAULT_QUALITY_REBUILD_LIMIT)
            all_metrics = metrics_from_calls(rows)
            return all_metrics.get(self._name, LLMMetrics(0, None, None))
        return LLMMetrics(0, None, None)

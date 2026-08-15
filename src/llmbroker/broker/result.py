"""Per-call result handle and the live per-LLM view returned by the broker."""

import logging
from collections.abc import Awaitable, Callable

from llmbroker.broker.pool import LLMPool
from llmbroker.models import LLMConfig, LLMMetrics, LLMState, Usage, check_score
from llmbroker.protocols.store import StoreProtocol

MetricsSource = Callable[[], Awaitable[dict[str, LLMMetrics]]]

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
        scope: str | None = None,
        observe_quality: Callable[[str, str | None, str, float], None] | None = None,
    ) -> None:
        self.text = text
        self.tool_calls = tool_calls
        self.usage = usage
        self._call_id = call_id
        self._llm_name = llm_name
        self._operation = operation
        self._store = store
        self._scope = scope
        self._observe_quality = observe_quality

    @property
    def llm_name(self) -> str:
        """Name of the model that answered — persist it to rate the call later."""
        return self._llm_name

    @property
    def operation(self) -> str | None:
        """Operation label passed to ask()/chat(), or None."""
        return self._operation

    @property
    def call_id(self) -> str:
        """Opaque id of this call; an optional passthrough for host analytics."""
        return self._call_id

    async def record_quality(self, score: float) -> None:
        """Rate the call this result came from — no journal read: the model, the
        operation and the call id are already here."""
        check_score(score)
        await self._store.record_quality(self._call_id, score, scope=self._scope)
        if self._observe_quality is not None:
            self._observe_quality(self._llm_name, self._operation, self._call_id, score)


class AsyncLLM:
    """Handle returned by ``AsyncBroker.get(name)`` — live view into broker internals."""

    def __init__(
        self,
        name: str,
        config: LLMConfig,
        pool: LLMPool,
        metrics_source: MetricsSource,
    ) -> None:
        self._name = name
        self._config = config
        self._pool = pool
        self._metrics_source = metrics_source

    @property
    def config(self) -> LLMConfig:
        return self._config

    @property
    def disabled(self) -> bool:
        return self._pool.is_disabled(self._name)

    async def state(self) -> LLMState:
        return self._pool.state(self._name)

    async def metrics(self) -> LLMMetrics:
        all_metrics = await self._metrics_source()
        return all_metrics.get(self._name, LLMMetrics(0, None, None))

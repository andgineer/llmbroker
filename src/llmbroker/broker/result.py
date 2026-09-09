"""Per-call result handle and the live per-LLM view returned by the broker."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, cast

from llmbroker.broker.pool import LLMPool
from llmbroker.models import LLMConfig, LLMMetrics, LLMState, Usage, check_score
from llmbroker.protocols.store import StoreProtocol

MetricsSource = Callable[[], Awaitable[dict[str, LLMMetrics]]]
ObserveQuality = Callable[[str, str | None, str, float], None]


class _StreamSource(Protocol):
    def __aiter__(self) -> AsyncIterator[str]: ...

    async def __anext__(self) -> str: ...

    async def another(self) -> "AsyncResult | None": ...

    async def aclose(self) -> None: ...


StreamStart = Callable[[], Awaitable[_StreamSource]]

logger = logging.getLogger("llmbroker.broker")

_UNRATEABLE = (
    "this call is not in the journal yet — a streamed call becomes rateable when its"
    " answer ends; close the stream first if you stopped reading early"
)


@dataclass(slots=True)
class CallReceipt:
    """What one routed call has settled so far: which model answered it, under what
    call id, and what it spent. ``settled`` goes up only once the journal row is
    written — a rating must never precede the row it names."""

    llm_name: str | None = None
    call_id: str | None = None
    usage: Usage | None = None
    settled: bool = False


class RoutedCall:
    """What every routed call hands back: the model that answered, and the rating that
    names it without a journal read."""

    def __init__(
        self,
        receipt: CallReceipt,
        *,
        operation: str | None,
        store: StoreProtocol,
        scope: str | None,
        observe_quality: ObserveQuality | None,
    ) -> None:
        self._receipt = receipt
        self._operation = operation
        self._store = store
        self._scope = scope
        self._observe_quality = observe_quality

    @property
    def llm_name(self) -> str | None:
        """Name of the model that answered — persist it to rate the call later."""
        return self._receipt.llm_name

    @property
    def call_id(self) -> str | None:
        """Opaque id of this call; an optional passthrough for host analytics."""
        return self._receipt.call_id

    @property
    def operation(self) -> str | None:
        """Operation label passed to the call, or None."""
        return self._operation

    @property
    def usage(self) -> Usage | None:
        """Token counts the provider reported, where it reported any."""
        return self._receipt.usage

    async def record_quality(self, score: float) -> None:
        """Rate the call this came from — no journal read: the model, the operation
        and the call id are already here. Raises ``ValueError`` until the call has
        settled, so a rating can never reach the store before the row it names."""
        check_score(score)
        receipt = self._receipt
        if not receipt.settled or receipt.llm_name is None or receipt.call_id is None:
            raise ValueError(_UNRATEABLE)
        await self._store.record_quality(receipt.call_id, score, scope=self._scope)
        if self._observe_quality is not None:
            self._observe_quality(receipt.llm_name, self._operation, receipt.call_id, score)


class AsyncResult(RoutedCall):
    """Returned by AsyncBroker.ask()/chat() — a call that has already answered."""

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
        observe_quality: ObserveQuality | None = None,
    ) -> None:
        super().__init__(
            CallReceipt(llm_name=llm_name, call_id=call_id, usage=usage, settled=True),
            operation=operation,
            store=store,
            scope=scope,
            observe_quality=observe_quality,
        )
        self.text = text
        self.tool_calls = tool_calls

    @property
    def llm_name(self) -> str:
        """Name of the model that answered — persist it to rate the call later."""
        return cast(str, self._receipt.llm_name)

    @property
    def call_id(self) -> str:
        """Opaque id of this call; an optional passthrough for host analytics."""
        return cast(str, self._receipt.call_id)


class StreamHandle(RoutedCall):
    """A streamed answer whose owner can supply further complete pool answers.
    Use ``aclosing`` so retained provider work is always settled."""

    def __init__(  # noqa: PLR0913
        self,
        start: StreamStart,
        receipt: CallReceipt,
        *,
        operation: str | None,
        store: StoreProtocol,
        scope: str | None,
        observe_quality: ObserveQuality | None,
    ) -> None:
        super().__init__(
            receipt,
            operation=operation,
            store=store,
            scope=scope,
            observe_quality=observe_quality,
        )
        self._start = start
        self._source: _StreamSource | None = None
        self._active: asyncio.Task[object] | None = None
        self._closed = False

    def __aiter__(self) -> "StreamHandle":
        return self

    async def __anext__(self) -> str:
        if self._closed:
            raise StopAsyncIteration
        if self._active is not None:
            raise RuntimeError("the stream already has an active pull or continuation")
        task = cast("asyncio.Task[object]", asyncio.current_task())
        self._active = task
        try:
            if self._source is None:
                self._source = await self._start()
            return await anext(self._source)
        finally:
            if self._active is task:
                self._active = None

    async def another(self) -> AsyncResult | None:
        """Return the next complete answer from this routed call, or ``None``."""
        if self._closed:
            raise RuntimeError("the stream is closed")
        if self._active is not None:
            raise RuntimeError("the stream already has an active pull or continuation")
        if self._source is None:
            raise RuntimeError("another answer requires a complete initial answer")
        task = cast("asyncio.Task[object]", asyncio.current_task())
        self._active = task
        try:
            return await self._source.another()
        finally:
            if self._active is task:
                self._active = None

    async def aclose(self) -> None:
        """Close all provider work and await its settlement."""
        self._closed = True
        active = self._active
        current = asyncio.current_task()
        if active is not None and active is not current:
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
        if self._source is not None:
            await self._source.aclose()


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

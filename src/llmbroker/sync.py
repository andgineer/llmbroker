"""Synchronous Broker / LLM / Result / Stream: blocking proxies that submit coroutines
to an ``AsyncBroker`` on a dedicated background event-loop thread."""

import asyncio
import logging
import threading
import weakref
from collections.abc import Callable, Coroutine, Mapping, Sequence
from concurrent.futures import CancelledError
from contextlib import suppress
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any

from llmbroker.broker.broker import (
    _DEFAULT_STATS_LIMIT,
    _DEFAULT_SYNC_INTERVAL,
    _SYNC_DEFAULT,
    AsyncBroker,
    _SyncDefault,
)
from llmbroker.broker.llms import AsyncLLMs
from llmbroker.broker.result import AsyncLLM, AsyncResult, StreamHandle
from llmbroker.direct import DirectClient
from llmbroker.models import (
    Call,
    LLMConfig,
    LLMMetrics,
    LLMState,
    LLMStats,
    PoolSnapshot,
    SyncReport,
    Usage,
)
from llmbroker.optimizer import Optimizer
from llmbroker.protocols.registry import RegistryProtocol
from llmbroker.protocols.secrets import SecretsProtocol
from llmbroker.protocols.store import StoreProtocol

logger = logging.getLogger("llmbroker.broker")


def _run_loop(loop: asyncio.AbstractEventLoop, _pinned: AsyncBroker) -> None:
    """Thread target: own ``loop`` until it is stopped, then close it. Top-level on
    purpose — a bound method would keep the ``Broker`` reachable and its finalizer could
    never fire."""
    # A daemon thread's frame outlives interpreter exit, so ``_pinned`` keeps its streams'
    # pending tasks from being freed then, each printing "Task was destroyed but pending".
    asyncio.set_event_loop(loop)
    try:
        loop.run_forever()
    finally:
        loop.close()


async def _teardown(broker: AsyncBroker) -> None:
    try:
        await broker.aclose()
    finally:
        await asyncio.get_running_loop().shutdown_asyncgens()


def _stopped(loop: asyncio.AbstractEventLoop, task: "asyncio.Task[None]") -> None:
    loop.stop()
    if not task.cancelled() and (exc := task.exception()) is not None:
        logger.error("closing a broker nobody closed failed", exc_info=exc)


def _stop_after_teardown(loop: asyncio.AbstractEventLoop, broker: AsyncBroker) -> None:
    loop.create_task(_teardown(broker)).add_done_callback(partial(_stopped, loop))


def _shutdown(loop: asyncio.AbstractEventLoop, broker: AsyncBroker) -> None:
    """The collector's backstop: schedule the teardown on the loop and return at once.
    Waiting could deadlock on a lock the collecting thread already holds."""
    loop.call_soon_threadsafe(_stop_after_teardown, loop, broker)


def _run_on(loop: asyncio.AbstractEventLoop, coro: Coroutine[Any, Any, Any]) -> Any:
    return asyncio.run_coroutine_threadsafe(coro, loop).result()


async def _pull(handle: StreamHandle) -> str | None:
    """The next delta, or ``None`` once the stream has ended."""
    try:
        return await anext(handle)
    except StopAsyncIteration:
        return None


def _close_on_loop(handle: StreamHandle) -> None:
    # The handle holds its own cleanup task, so this one needs no reference kept.
    asyncio.ensure_future(handle.aclose())


def _abandon(loop: asyncio.AbstractEventLoop, handle: StreamHandle) -> None:
    """Close a stream nobody closed, without waiting: a finalizer may run on the loop's
    own thread, where waiting on the loop would deadlock."""
    if loop.is_closed():
        return
    with suppress(RuntimeError):  # the loop closed after the check: nothing to cancel
        loop.call_soon_threadsafe(_close_on_loop, handle)


class Result:
    """Synchronous analogue of AsyncResult."""

    def __init__(self, run_fn: "Callable[[Any], Any]", async_result: AsyncResult) -> None:
        self._run = run_fn
        self._async = async_result
        self.text = async_result.text
        self.tool_calls = async_result.tool_calls
        self.usage = async_result.usage

    @property
    def llm_name(self) -> str:
        return self._async.llm_name

    @property
    def operation(self) -> str | None:
        return self._async.operation

    @property
    def call_id(self) -> str:
        return self._async.call_id

    def record_quality(self, score: float) -> None:
        self._run(self._async.record_quality(score))


class LLM:
    """Synchronous analogue of AsyncLLM."""

    def __init__(self, run_fn: "Callable[[Any], Any]", async_llm: AsyncLLM) -> None:
        self._run = run_fn
        self._async = async_llm

    @property
    def config(self) -> LLMConfig:
        return self._async.config

    @property
    def disabled(self) -> bool:
        return self._async.disabled

    def state(self) -> LLMState:
        return self._run(self._async.state())

    def metrics(self) -> LLMMetrics:
        return self._run(self._async.metrics())


class Stream:
    """Synchronous analogue of StreamHandle. Each pull runs on the broker's loop, so it
    raises what the async stream raises at that pull; closing it, or dropping it
    unclosed, closes the async stream and so cancels the provider request."""

    def __init__(
        self,
        broker: "Broker",
        loop: asyncio.AbstractEventLoop,
        handle: StreamHandle,
    ) -> None:
        self._broker = broker  # a live stream keeps its broker, and so its loop, alive
        self._run = partial(_run_on, loop)
        self._async = handle
        self._finalizer = weakref.finalize(self, _abandon, loop, handle)
        # At exit the loop thread dies with the process; a close scheduled then never runs.
        self._finalizer.atexit = False

    @property
    def llm_name(self) -> str | None:
        return self._async.llm_name

    @property
    def call_id(self) -> str | None:
        return self._async.call_id

    @property
    def operation(self) -> str | None:
        return self._async.operation

    @property
    def usage(self) -> Usage | None:
        return self._async.usage

    def __iter__(self) -> "Stream":
        return self

    def __next__(self) -> str:
        if not self._finalizer.alive or self._async.closed:
            raise StopIteration
        try:
            delta = self._run(_pull(self._async))
        except CancelledError:
            # A close of this stream or of the broker, made from another thread, ends it.
            if self._async.closed:
                raise StopIteration from None
            raise
        if delta is None:
            raise StopIteration
        return delta

    def another(self) -> Result | None:
        answer = self._run(self._async.another())
        return None if answer is None else Result(self._run, answer)

    def record_quality(self, score: float) -> None:
        self._run(self._async.record_quality(score))

    def close(self) -> None:
        """Close all provider work and wait for its settlement."""
        if self._finalizer.detach() is None or self._async.closed:
            return
        self._run(self._async.aclose())

    def __enter__(self) -> "Stream":
        if not self._finalizer.alive or self._async.closed:
            raise RuntimeError("the stream is closed")
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class LLMs:
    """Synchronous analogue of AsyncLLMs — one caller over the shared pool."""

    def __init__(
        self,
        broker: "Broker",
        loop: asyncio.AbstractEventLoop,
        async_llms: AsyncLLMs,
    ) -> None:
        self._broker = broker  # a live caller keeps its broker, and so its loop, alive
        self._loop = loop
        self._run = partial(_run_on, loop)
        self._async = async_llms

    @property
    def scope(self) -> str | None:
        return self._async.scope

    def ask(  # noqa: PLR0913 - the call knobs, one keyword each
        self,
        prompt: str,
        *,
        operation: str | None = None,
        trace_id: str | None = None,
        wait: float | None = None,
        fastest_of: int | None = None,
        parallel_recovery: bool = True,
        response_format: dict | None = None,
    ) -> Result:
        return Result(
            self._run,
            self._run(
                self._async.ask(
                    prompt,
                    operation=operation,
                    trace_id=trace_id,
                    wait=wait,
                    fastest_of=fastest_of,
                    parallel_recovery=parallel_recovery,
                    response_format=response_format,
                ),
            ),
        )

    def chat(  # noqa: PLR0913 - what to send, and the call knobs
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        operation: str | None = None,
        trace_id: str | None = None,
        wait: float | None = None,
        fastest_of: int | None = None,
        parallel_recovery: bool = True,
        response_format: dict | None = None,
    ) -> Result:
        return Result(
            self._run,
            self._run(
                self._async.chat(
                    messages,
                    tools=tools,
                    operation=operation,
                    trace_id=trace_id,
                    wait=wait,
                    fastest_of=fastest_of,
                    parallel_recovery=parallel_recovery,
                    response_format=response_format,
                ),
            ),
        )

    def stream(  # noqa: PLR0913 - the call knobs, one keyword each
        self,
        prompt: str,
        *,
        operation: str | None = None,
        trace_id: str | None = None,
        wait: float | None = None,
        fastest_of: int | None = None,
        parallel_recovery: bool = True,
        response_format: dict | None = None,
        stream_selection_window: float = 1.0,
    ) -> Stream:
        """Return an owned stream that can supply another complete pool answer. The
        broker closes it; ``with`` or ``close()`` releases it earlier."""
        return Stream(
            self._broker,
            self._loop,
            self._async.stream(
                prompt,
                operation=operation,
                trace_id=trace_id,
                wait=wait,
                fastest_of=fastest_of,
                parallel_recovery=parallel_recovery,
                response_format=response_format,
                stream_selection_window=stream_selection_window,
            ),
        )

    def direct(self, alias: str | None = None, *, name: str | None = None) -> DirectClient:
        """Return a synchronous direct client for a declared model — same alias/name
        keyspaces and errors as the async counterpart."""
        cfg, key = self._run(self._async.resolve_direct(alias, name=name))
        return DirectClient(
            base_url=cfg.base_url,
            model=cfg.model,
            api_key=key,
            tool_params=cfg.tool_params,
        )

    def get(self, name: str) -> LLM:
        return LLM(self._run, self._run(self._async.get(name)))

    def count(self) -> int:
        return self._run(self._async.count())

    def record_quality(
        self,
        score: float,
        *,
        call_id: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        self._run(self._async.record_quality(score, call_id=call_id, trace_id=trace_id))

    def calls(
        self,
        *,
        limit: int,
        since: datetime | None = None,
        operation: str | None = None,
        trace_id: str | None = None,
        call_id: str | None = None,
    ) -> list[Call]:
        return self._run(
            self._async.calls(
                limit=limit,
                since=since,
                operation=operation,
                trace_id=trace_id,
                call_id=call_id,
            ),
        )

    def stats(
        self,
        *,
        since: datetime | None = None,
        limit: int = _DEFAULT_STATS_LIMIT,
        operation: str | None = None,
    ) -> Mapping[str, LLMStats]:
        return self._run(self._async.stats(since=since, limit=limit, operation=operation))


class Broker:
    """Synchronous client over an AsyncBroker on a background loop thread."""

    def __init__(  # noqa: PLR0913
        self,
        registry: RegistryProtocol | str | Path | None = None,
        *,
        secrets: SecretsProtocol | None = None,
        store: StoreProtocol | None = None,
        optimize: bool | Optimizer = True,
        sync: str | None | _SyncDefault = _SYNC_DEFAULT,
        sync_interval: float | None = _DEFAULT_SYNC_INTERVAL,
        home: str | Path | None = None,
        direct: Sequence[str | LLMConfig] = (),
    ) -> None:
        self._async = AsyncBroker(
            registry,
            secrets=secrets,
            store=store,
            optimize=optimize,
            sync=sync,
            sync_interval=sync_interval,
            home=home,
            direct=direct,
        )
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=_run_loop,
            args=(self._loop, self._async),
            daemon=True,
            name="llmbroker-loop",
        )
        self._thread.start()
        # Backstop for a Broker nobody closes. The callback never holds self, so it does
        # not pin the instance it is registered on.
        self._finalizer = weakref.finalize(self, _shutdown, self._loop, self._async)
        # At exit the executors are already shut, so a teardown could journal nothing.
        self._finalizer.atexit = False
        self._run = partial(_run_on, self._loop)

    @property
    def llms(self) -> "LLMs":
        # Made on each access: stored, it would hold self in a cycle only the cycle
        # collector could break, and the teardown would then run on whatever thread it hits.
        return LLMs(self, self._loop, self._async.llms)

    def for_scope(self, scope: str) -> "LLMs":
        """A caller that pays with ``scope``\'s own keys and writes ``scope`` on every
        row it journals. Costs no I/O."""
        return LLMs(self, self._loop, self._async.for_scope(scope))

    def ensure_pool(self) -> None:
        """Provision now rather than on the first call that routes — the eager fail-fast."""
        self._run(self._async.ensure_pool())

    # ── The unscoped caller, delegated ──
    def get(self, name: str) -> LLM:
        return self.llms.get(name)

    def count(self) -> int:
        return self.llms.count()

    def ask(  # noqa: PLR0913 - the call knobs, one keyword each
        self,
        prompt: str,
        *,
        operation: str | None = None,
        trace_id: str | None = None,
        wait: float | None = None,
        fastest_of: int | None = None,
        parallel_recovery: bool = True,
        response_format: dict | None = None,
    ) -> Result:
        return self.llms.ask(
            prompt,
            operation=operation,
            trace_id=trace_id,
            wait=wait,
            fastest_of=fastest_of,
            parallel_recovery=parallel_recovery,
            response_format=response_format,
        )

    def chat(  # noqa: PLR0913 - what to send, and the call knobs
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        operation: str | None = None,
        trace_id: str | None = None,
        wait: float | None = None,
        fastest_of: int | None = None,
        parallel_recovery: bool = True,
        response_format: dict | None = None,
    ) -> Result:
        return self.llms.chat(
            messages,
            tools=tools,
            operation=operation,
            trace_id=trace_id,
            wait=wait,
            fastest_of=fastest_of,
            parallel_recovery=parallel_recovery,
            response_format=response_format,
        )

    def stream(  # noqa: PLR0913 - the call knobs, one keyword each
        self,
        prompt: str,
        *,
        operation: str | None = None,
        trace_id: str | None = None,
        wait: float | None = None,
        fastest_of: int | None = None,
        parallel_recovery: bool = True,
        response_format: dict | None = None,
        stream_selection_window: float = 1.0,
    ) -> Stream:
        return self.llms.stream(
            prompt,
            operation=operation,
            trace_id=trace_id,
            wait=wait,
            fastest_of=fastest_of,
            parallel_recovery=parallel_recovery,
            response_format=response_format,
            stream_selection_window=stream_selection_window,
        )

    def direct(self, alias: str | None = None, *, name: str | None = None) -> DirectClient:
        return self.llms.direct(alias, name=name)

    def record_quality(
        self,
        score: float,
        *,
        call_id: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        self.llms.record_quality(score, call_id=call_id, trace_id=trace_id)

    def snapshot(self) -> PoolSnapshot:
        return self._run(self._async.snapshot())

    def sync(self, source: str | None = None) -> SyncReport | None:
        return self._run(self._async.sync(source))

    @property
    def last_sync_report(self) -> SyncReport | None:
        return self._async.last_sync_report

    def disable_llm(self, name: str) -> None:
        self._run(self._async.disable_llm(name))

    def enable_llm(self, name: str) -> None:
        self._run(self._async.enable_llm(name))

    def calls(
        self,
        *,
        limit: int,
        since: datetime | None = None,
        operation: str | None = None,
        trace_id: str | None = None,
        call_id: str | None = None,
    ) -> list[Call]:
        return self.llms.calls(
            limit=limit,
            since=since,
            operation=operation,
            trace_id=trace_id,
            call_id=call_id,
        )

    def stats(
        self,
        *,
        since: datetime | None = None,
        limit: int = _DEFAULT_STATS_LIMIT,
        operation: str | None = None,
    ) -> Mapping[str, LLMStats]:
        return self.llms.stats(since=since, limit=limit, operation=operation)

    # ── lifecycle ──
    def close(self) -> None:
        """Close every owned stream and wait until each is settled and journaled."""
        if self._finalizer.detach() is None:
            return
        try:
            self._run(_teardown(self._async))
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join()

    def __enter__(self) -> "Broker":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

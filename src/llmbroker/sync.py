"""Synchronous Broker / LLM / Result: blocking proxies that submit coroutines to an
``AsyncBroker`` on a dedicated background event-loop thread."""

import asyncio
import threading
import weakref
from collections.abc import Callable, Coroutine, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from llmbroker.broker.broker import (
    _DEFAULT_STATS_LIMIT,
    _DEFAULT_SYNC_INTERVAL,
    _SYNC_DEFAULT,
    AsyncBroker,
    _SyncDefault,
)
from llmbroker.broker.result import AsyncLLM, AsyncResult
from llmbroker.direct import DirectClient
from llmbroker.models import (
    Call,
    LLMConfig,
    LLMMetrics,
    LLMState,
    LLMStats,
    PoolSnapshot,
    SyncReport,
)
from llmbroker.optimizer import Optimizer
from llmbroker.protocols.registry import RegistryProtocol
from llmbroker.protocols.secrets import SecretsProtocol
from llmbroker.protocols.store import StoreProtocol


def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Thread target: own ``loop`` until it is stopped. Top-level on purpose — a bound
    method would keep the ``Broker`` reachable and its finalizer could never fire."""
    asyncio.set_event_loop(loop)
    loop.run_forever()


def _shutdown(loop: asyncio.AbstractEventLoop, thread: threading.Thread) -> None:
    """Stop the background loop and reclaim its thread. Runs once."""
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5.0)
    loop.close()


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


class Broker:
    """Synchronous client over an AsyncBroker on a background loop thread."""

    def __init__(  # noqa: PLR0913
        self,
        registry: RegistryProtocol | str | Path | None = None,
        *,
        secrets: SecretsProtocol | None = None,
        store: StoreProtocol | None = None,
        optimize: bool | Optimizer = True,
        scope: str | None = None,
        have_keys: bool | Sequence[str] = False,
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
            scope=scope,
            have_keys=have_keys,
            sync=sync,
            sync_interval=sync_interval,
            home=home,
            direct=direct,
        )
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=_run_loop,
            args=(self._loop,),
            daemon=True,
            name="llmbroker-loop",
        )
        self._thread.start()
        # Backstop for a Broker nobody closes. The callback holds only loop + thread,
        # never self, so it does not pin the instance it is registered on.
        self._finalizer = weakref.finalize(self, _shutdown, self._loop, self._thread)

    def _run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def _ensure_pool(self) -> None:
        self._run(self._async.ensure_pool())

    # ── Accessors ──
    def get(self, name: str) -> LLM:
        return LLM(self._run, self._run(self._async.get(name)))

    def count(self) -> int:
        return self._run(self._async.count())

    # ── calls ──
    def ask(
        self,
        prompt: str,
        *,
        operation: str | None = None,
        trace_id: str | None = None,
        wait: float | None = None,
    ) -> Result:
        return Result(
            self._run,
            self._run(self._async.ask(prompt, operation=operation, trace_id=trace_id, wait=wait)),
        )

    def chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        operation: str | None = None,
        trace_id: str | None = None,
        wait: float | None = None,
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
                ),
            ),
        )

    def direct(self, alias: str | None = None, *, name: str | None = None) -> DirectClient:
        """Return a synchronous direct client (``ask()`` only) for a declared model.

        Streaming is async-only; use ``AsyncBroker.direct`` for deltas. Same
        alias/name keyspaces and errors as the async counterpart.
        """
        cfg, key = self._run(self._async._resolve_direct(alias, name=name))  # noqa: SLF001
        return DirectClient(base_url=cfg.base_url, model=cfg.model, api_key=key)

    def record_quality(
        self,
        llm_name: str,
        operation: str | None,
        score: float,
        *,
        call_id: str | None = None,
    ) -> None:
        self._run(self._async.record_quality(llm_name, operation, score, call_id=call_id))

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
        kind: str | None = None,
        operation: str | None = None,
    ) -> list[Call]:
        return self._run(
            self._async.calls(limit=limit, since=since, kind=kind, operation=operation),
        )

    def stats(
        self,
        *,
        since: datetime | None = None,
        limit: int = _DEFAULT_STATS_LIMIT,
        operation: str | None = None,
    ) -> Mapping[str, LLMStats]:
        return self._run(self._async.stats(since=since, limit=limit, operation=operation))

    # ── lifecycle ──
    def close(self) -> None:
        if not self._finalizer.alive:
            return
        self._run(self._async.aclose())
        # Run the same teardown the GC backstop would, and mark it done so the
        # finalizer does not repeat it later.
        self._finalizer()

    def __enter__(self) -> "Broker":
        self._ensure_pool()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

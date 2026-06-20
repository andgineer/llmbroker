"""Synchronous Broker / LLM / Result — blocking proxies over AsyncBroker.

``Broker`` runs an ``AsyncBroker`` on a dedicated background event-loop thread;
its blocking methods submit coroutines to that loop and wait. The pool's
concurrency persists across calls.
"""

import asyncio
import threading
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import Future
from datetime import datetime
from typing import Any

from llmbroker.broker import AsyncBroker, AsyncResult, Optimizer
from llmbroker.models import (
    Call,
    LLMConfig,
    LLMMetrics,
    LLMSnapshot,
    LLMState,
    SyncPolicy,
)
from llmbroker.registry import RegistryProtocol
from llmbroker.secrets import SecretsProtocol
from llmbroker.shared_state import SharedStateProtocol
from llmbroker.telemetry import TelemetryProtocol


class Result:
    """Synchronous analogue of AsyncResult."""

    def __init__(self, run_fn: "Callable[[Any], Any]", async_result: AsyncResult) -> None:
        self._run = run_fn
        self._async = async_result
        self.text = async_result.text
        self.tool_calls = async_result.tool_calls
        self.usage = async_result.usage

    def record_quality(self, score: float) -> None:
        self._run(self._async.record_quality(score))


class LLM:
    """Synchronous analogue of AsyncLLM."""

    def __init__(self, broker: "Broker", name: str) -> None:
        self._broker = broker
        self._name = name

    @property
    def config(self) -> LLMConfig:
        return self._broker.config_of(self._name)

    def state(self) -> LLMState:
        return self._broker.state_of(self._name)

    def metrics(self, *, since: datetime | None = None) -> LLMMetrics:
        return self._broker.metrics_of(self._name, since=since)


class Broker(Mapping[str, LLM]):
    """Shipped synchronous client over an AsyncBroker on a background loop thread."""

    def __init__(
        self,
        *,
        registry: RegistryProtocol,
        secrets: SecretsProtocol | None = None,
        shared_state: SharedStateProtocol | None = None,
        telemetry: TelemetryProtocol | None = None,
        optimize: bool | Optimizer = True,
    ) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="llmbroker-loop")
        self._thread.start()
        self._async = self._make_async(
            registry=registry,
            secrets=secrets,
            shared_state=shared_state,
            telemetry=telemetry,
            optimize=optimize,
        )
        self._closed = False

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _make_async(self, **kwargs) -> AsyncBroker:
        # AsyncBroker.__init__ creates an asyncio.Queue, which binds to the
        # running loop — construct it on the broker's own loop thread.
        fut: Future[AsyncBroker] = Future()

        def _build() -> None:
            try:
                fut.set_result(AsyncBroker(**kwargs))
            except BaseException as exc:  # noqa: BLE001
                fut.set_exception(exc)

        self._loop.call_soon_threadsafe(_build)
        return fut.result()

    def _run(self, coro) -> Any:  # noqa: ANN001
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def _ensure_started(self) -> None:
        # The async pool starts lazily on the first ask/chat; the sync Mapping
        # surface (len/iter) must reflect loaded config without a call, so warm it.
        self._run(self._async.ensure_started())

    # ── Mapping interface ──
    def __getitem__(self, name: str) -> LLM:
        self._ensure_started()
        if name not in self._async:
            raise KeyError(name)
        return LLM(self, name)

    def __iter__(self) -> Iterator[str]:
        self._ensure_started()
        return iter(self._async)

    def __len__(self) -> int:
        self._ensure_started()
        return len(self._async)

    # ── LLM accessors (used by LLM companion class) ──
    def config_of(self, name: str) -> LLMConfig:
        self._ensure_started()
        return self._async[name].config

    def state_of(self, name: str) -> LLMState:
        return self._run(self._async[name].state())

    def metrics_of(self, name: str, *, since: datetime | None = None) -> LLMMetrics:
        return self._run(self._async[name].metrics(since=since))

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

    def snapshot(self, *, since: datetime | None = None) -> Mapping[str, LLMSnapshot]:
        return self._run(self._async.snapshot(since=since))

    def add(self, cfg: LLMConfig) -> None:
        self._run(self._async.add(cfg))

    def remove(self, name: str) -> None:
        self._run(self._async.remove(name))

    def sync_configs(self, source: RegistryProtocol, *, policy: SyncPolicy = "mirror") -> None:
        self._run(self._async.sync_configs(source, policy=policy))

    def calls(self, *, limit: int) -> list[Call]:
        return self._run(self._async.calls(limit=limit))

    def purge_calls(self, *, before: datetime) -> int:
        return self._run(self._async.purge_calls(before=before))

    def alerts(self) -> list:
        return self._run(self._async.alerts())

    # ── lifecycle ──
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._run(self._async.aclose())
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)
        self._loop.close()

    def __enter__(self) -> "Broker":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

"""Synchronous Broker / LLM / Result — blocking proxies over AsyncBroker.

``Broker`` runs an ``AsyncBroker`` on a dedicated background event-loop thread;
its blocking methods submit coroutines to that loop and wait. The pool's
concurrency persists across calls.
"""

import asyncio
import threading
import weakref
from collections.abc import Callable, Coroutine, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from llmbroker.broker import AsyncBroker, AsyncResult
from llmbroker.models import Call, LLMConfig, LLMMetrics, LLMSnapshot, LLMState, SeedPolicy
from llmbroker.optimizer import Optimizer
from llmbroker.protocols.backend_stack import BackendStack
from llmbroker.protocols.registry import RegistryProtocol
from llmbroker.protocols.secrets import SecretsProtocol
from llmbroker.protocols.telemetry import TelemetryProtocol
from llmbroker.standalone.registry import Registry


def _run_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Thread target: own ``loop`` until it is stopped.

    Top-level (not a bound method) on purpose: the background thread must not
    hold a reference to the ``Broker`` instance, or the running thread would
    keep the instance reachable forever and its ``weakref.finalize`` cleanup
    could never fire.
    """
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

    def metrics(self) -> LLMMetrics:
        return self._broker.metrics_of(self._name)


class Broker:
    """Synchronous client over an AsyncBroker on a background loop thread."""

    def __init__(  # noqa: PLR0913
        self,
        registry: RegistryProtocol | str | Path | None = None,
        *,
        stack: BackendStack | None = None,
        secrets: SecretsProtocol | None = None,
        telemetry: TelemetryProtocol | None = None,
        optimize: bool | Optimizer = True,
        seed: RegistryProtocol | str | Path | None = None,
        seed_policy: SeedPolicy = SeedPolicy.SYNC,
        scope: str | None = None,
    ) -> None:
        if isinstance(registry, (str, Path)):
            registry = Registry(registry)
        if isinstance(seed, (str, Path)):
            seed = Registry(seed)
        self._async = AsyncBroker(
            registry,
            stack=stack,
            secrets=secrets,
            telemetry=telemetry,
            optimize=optimize,
            seed=seed,
            seed_policy=seed_policy,
            scope=scope,
        )
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=_run_loop,
            args=(self._loop,),
            daemon=True,
            name="llmbroker-loop",
        )
        self._thread.start()
        # Backstop: if the caller never closes the Broker, stop the loop and
        # join the thread when the instance is garbage-collected. The callback
        # holds only loop + thread (never self), so it does not pin the Broker.
        self._finalizer = weakref.finalize(self, _shutdown, self._loop, self._thread)

    def _run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def _ensure_pool(self) -> None:
        self._run(self._async.ensure_pool())

    # ── Accessors ──
    def get(self, name: str) -> LLM:
        self._run(self._async.get(name))  # raises KeyError if absent
        return LLM(self, name)

    def count(self) -> int:
        return self._run(self._async.count())

    # ── LLM accessors (used by LLM companion class) ──
    def config_of(self, name: str) -> LLMConfig:
        return self._run(self._async.get(name)).config

    def state_of(self, name: str) -> LLMState:
        llm = self._run(self._async.get(name))
        return self._run(llm.state())

    def metrics_of(self, name: str) -> LLMMetrics:
        llm = self._run(self._async.get(name))
        return self._run(llm.metrics())

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

    def snapshot(self) -> Mapping[str, LLMSnapshot]:
        return self._run(self._async.snapshot())

    def add(self, cfg: LLMConfig) -> None:
        self._run(self._async.add(cfg))

    def update(self, cfg: LLMConfig) -> None:
        self._run(self._async.update(cfg))

    def remove(self, name: str) -> None:
        self._run(self._async.remove(name))

    def disable_llm(self, name: str) -> None:
        self._run(self._async.disable_llm(name))

    def enable_llm(self, name: str) -> None:
        self._run(self._async.enable_llm(name))

    def calls(self, *, limit: int) -> list[Call]:
        return self._run(self._async.calls(limit=limit))

    def purge_calls(self, *, before: datetime) -> int:
        return self._run(self._async.purge_calls(before=before))

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

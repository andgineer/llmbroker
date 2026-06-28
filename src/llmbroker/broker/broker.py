"""The ``AsyncBroker`` façade over the LLM pool and its collaborators.

``AsyncBroker`` owns the external ports (registry, secrets, telemetry, state
store), lazily provisions the live ``LLMPool`` once, and delegates each
operation to the collaborator that owns it:

* ``Catalog``  — pool membership in sync with the registry (seed/load + edits)
* ``Router``   — routing a completion over the pool with failover
* ``PoolView`` — read-only views of current pool state

The call journal (``calls``/``purge_calls``) is a thin pass-through to a queryable
telemetry backend; ``alerts`` is delegated to ``Optimizer``.
"""

import asyncio
import contextlib
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import cast

from llmbroker.broker.catalog import Catalog
from llmbroker.broker.pool import LLMPool
from llmbroker.broker.pool_view import PoolView
from llmbroker.broker.result import AsyncLLM, AsyncResult
from llmbroker.broker.router import Router
from llmbroker.models import (
    Alert,
    AsyncResourceProtocol,
    Call,
    LifecyclePhase,
    LLMConfig,
    LLMSnapshot,
    SeedPolicy,
)
from llmbroker.optimizer import Optimizer, OptimizerTelemetry
from llmbroker.protocols.registry import RegistryProtocol
from llmbroker.protocols.secrets import SecretsProtocol
from llmbroker.protocols.state_store import StateStoreProtocol
from llmbroker.protocols.telemetry import QueryableTelemetryProtocol, TelemetryProtocol
from llmbroker.standalone.registry import Registry
from llmbroker.standalone.secrets import Secrets, as_secrets
from llmbroker.standalone.telemetry import Telemetry


class AsyncBroker:
    """Façade over the LLM pool: route completions, inspect state, edit the catalog."""

    def __init__(  # noqa: PLR0913
        self,
        registry: RegistryProtocol | str | Path,
        *,
        secrets: SecretsProtocol | None = None,
        state_store: StateStoreProtocol | None = None,
        telemetry: TelemetryProtocol | None = None,
        optimize: bool | Optimizer = True,
        seed: RegistryProtocol | str | Path | None = None,
        seed_policy: SeedPolicy = SeedPolicy.IF_EMPTY,
        user_id: int | str | None = None,
    ) -> None:
        registry = Registry(registry) if isinstance(registry, (str, Path)) else registry
        secrets = as_secrets(secrets) if secrets is not None else Secrets()
        telemetry = telemetry if telemetry is not None else Telemetry()
        seed = Registry(seed) if isinstance(seed, (str, Path)) else seed

        if isinstance(optimize, Optimizer):
            self._optimizer: Optimizer | None = optimize
        elif optimize:
            self._optimizer = Optimizer()
        else:
            self._optimizer = None

        self._registry = registry
        self._secrets = secrets
        self._base_telemetry = telemetry
        self._state_store = state_store
        self._user_id = user_id

        pool = LLMPool(state_store, user_id)
        self._pool = pool
        self._catalog = Catalog(
            registry,
            secrets,
            pool,
            seed=seed,
            seed_policy=seed_policy,
            user_id=user_id,
        )

        if self._optimizer is not None:
            effective_telemetry: TelemetryProtocol = OptimizerTelemetry(
                self._optimizer,
                telemetry,
                pool,
                on_go_offline=self._on_go_offline,
            )
        else:
            effective_telemetry = telemetry

        self._telemetry = effective_telemetry
        self._router = Router(pool, effective_telemetry, user_id=user_id, optimizer=self._optimizer)
        self._pool_view = PoolView(pool, effective_telemetry, user_id=user_id)

        self._provisioned = False
        self._provision_lock = asyncio.Lock()
        self._bg_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def ensure_pool(self) -> None:
        """Lazy idempotent initializer — provisions the pool exactly once."""
        if self._provisioned:
            return
        async with self._provision_lock:
            if self._provisioned:
                return
            await self._catalog.provision()
            if self._optimizer is not None and isinstance(
                self._base_telemetry,
                QueryableTelemetryProtocol,
            ):
                metrics = await self._base_telemetry.metrics()
                self._optimizer.seed_from_metrics(metrics)
            self._provisioned = True

    async def aclose(self) -> None:
        tasks = list(self._bg_tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._bg_tasks.clear()
        for port in (self._registry, self._secrets, self._telemetry, self._state_store):
            if isinstance(port, AsyncResourceProtocol):
                await port.aclose()

    async def __aenter__(self) -> "AsyncBroker":
        await self.ensure_pool()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    async def ask(
        self,
        prompt: str,
        *,
        operation: str | None = None,
        trace_id: str | None = None,
        wait: float | None = None,
    ) -> AsyncResult:
        await self.ensure_pool()
        return await self._router.ask(prompt, operation=operation, trace_id=trace_id, wait=wait)

    async def chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        operation: str | None = None,
        trace_id: str | None = None,
        wait: float | None = None,
    ) -> AsyncResult:
        await self.ensure_pool()
        return await self._router.chat(
            messages,
            tools=tools,
            operation=operation,
            trace_id=trace_id,
            wait=wait,
        )

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    async def get(self, name: str) -> AsyncLLM:
        await self.ensure_pool()
        return self._pool_view.get(name)

    async def count(self) -> int:
        await self.ensure_pool()
        return self._pool_view.count()

    async def snapshot(self, *, since: datetime | None = None) -> Mapping[str, LLMSnapshot]:
        await self.ensure_pool()
        return await self._pool_view.snapshot(since=since)

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    async def add(self, cfg: LLMConfig) -> None:
        await self.ensure_pool()
        await self._catalog.add(cfg)

    async def update(self, cfg: LLMConfig) -> None:
        await self.ensure_pool()
        await self._catalog.update(cfg)

    async def remove(self, name: str) -> None:
        await self.ensure_pool()
        await self._catalog.remove(name)

    # ------------------------------------------------------------------
    # Call journal / retention
    # ------------------------------------------------------------------

    async def calls(self, *, limit: int) -> list[Call]:
        return await self._require_queryable().calls(limit=limit, user_id=self._user_id)

    async def purge_calls(self, *, before: datetime) -> int:
        return await self._require_queryable().purge_calls(before=before)

    async def alerts(self) -> list[Alert]:
        if self._optimizer is None:
            return []
        return self._optimizer.alerts()

    def _on_go_offline(self, llm_name: str) -> None:
        if any(t.get_name() == f"probe-{llm_name}" and not t.done() for t in self._bg_tasks):
            return
        task = asyncio.create_task(self._probe_loop(llm_name), name=f"probe-{llm_name}")
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _probe_loop(self, llm_name: str) -> None:
        assert self._optimizer is not None
        await asyncio.sleep(self._optimizer.offline_sleep)
        if (
            llm_name not in self._pool
            or self._pool.state(llm_name).phase is not LifecyclePhase.OFFLINE
        ):
            return
        self._pool.set_probing(llm_name)
        self._optimizer.on_probing_start(llm_name)
        config = self._pool.config(llm_name)
        self._pool.release(config)

    def _require_queryable(self) -> QueryableTelemetryProtocol:
        if not isinstance(self._base_telemetry, QueryableTelemetryProtocol):
            raise TypeError(
                "this telemetry backend is not queryable — use a queryable backend"
                " (e.g. llmbroker.sqlite.Telemetry) for calls()/purge_calls()",
            )
        return cast(QueryableTelemetryProtocol, self._telemetry)

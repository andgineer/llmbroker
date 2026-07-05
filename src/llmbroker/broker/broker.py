"""The ``AsyncBroker`` façade over the LLM pool and its collaborators.

``AsyncBroker`` owns the external ports (registry, secrets, telemetry),
lazily provisions the live ``LLMPool`` once, and delegates each operation to
the collaborator that owns it:

* ``Catalog``       — pool membership in sync with the registry (seed/load + edits)
* ``Router``        — routing a completion over the pool with failover
* ``PoolView``       — read-only views of current pool state
* ``_LearningHook`` — quality windows, dead-key drops, and the debounced
  journal rebuild feeding shared cooldowns, snapshot metrics, and the admin
  disabled-verdict map (only wired when ``optimize`` is truthy)

The call journal (``calls``/``purge_calls``) is a thin pass-through to a queryable
telemetry backend; ``alerts`` is delegated to ``Optimizer``.
"""

import asyncio
import logging
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import cast

from llmbroker.broker.catalog import Catalog
from llmbroker.broker.learning import _LearningHook
from llmbroker.broker.pool import LLMPool
from llmbroker.broker.pool_view import PoolView
from llmbroker.broker.result import AsyncLLM, AsyncResult
from llmbroker.broker.router import Router
from llmbroker.exceptions import NoLLMAvailableError
from llmbroker.models import (
    Alert,
    AsyncResourceProtocol,
    Call,
    LifecyclePhase,
    LLMConfig,
    LLMSnapshot,
    SeedPolicy,
)
from llmbroker.optimizer import Optimizer
from llmbroker.protocols.backend_stack import BackendStack
from llmbroker.protocols.registry import RegistryProtocol
from llmbroker.protocols.secrets import SecretsProtocol
from llmbroker.protocols.telemetry import (
    DisabledMapProtocol,
    QueryableTelemetryProtocol,
    TelemetryProtocol,
)
from llmbroker.standalone.registry import Registry
from llmbroker.standalone.secrets import Secrets, as_secrets
from llmbroker.standalone.telemetry import Telemetry

logger = logging.getLogger("llmbroker.broker")


class AsyncBroker:
    """Façade over the LLM pool: route completions, inspect state, edit the catalog."""

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
        if scope == "":
            raise ValueError("scope must not be empty string; use None for unscoped")
        if registry is None and stack is None:
            raise ValueError("AsyncBroker requires either `registry` or `stack`")

        if registry is None:
            assert stack is not None
            registry = stack.registry
        elif isinstance(registry, (str, Path)):
            registry = Registry(registry)

        secrets = (
            as_secrets(secrets)
            if secrets is not None
            else (stack.secrets if stack is not None else Secrets())
        )
        telemetry = (
            telemetry
            if telemetry is not None
            else (stack.telemetry if stack is not None else Telemetry())
        )
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
        self._scope = scope

        pool = LLMPool(optimizer=self._optimizer)
        self._pool = pool
        self._catalog = Catalog(
            registry,
            secrets,
            pool,
            seed=seed,
            seed_policy=seed_policy,
            scope=scope,
        )

        self._learning_hook: _LearningHook | None = None
        effective_telemetry: TelemetryProtocol
        if self._optimizer is not None:
            self._learning_hook = _LearningHook(
                self._optimizer,
                telemetry,
                pool,
                self._catalog.resync,
            )
            effective_telemetry = self._learning_hook
        else:
            effective_telemetry = telemetry

        self._telemetry = effective_telemetry
        self._router = Router(pool, effective_telemetry, scope=scope, optimizer=self._optimizer)
        self._pool_view = PoolView(pool, effective_telemetry)

        self._provisioned = False
        self._provision_lock = asyncio.Lock()
        self._last_underprov_alert: float = float("-inf")
        self._underprov_alert_interval: float = 60.0

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
            seed_alerts = await self._catalog.provision()
            if self._optimizer is not None:
                for msg in seed_alerts:
                    self._optimizer.add_alert(msg)
            if self._learning_hook is not None:
                # warm start — provision() above already resynced the registry
                await self._learning_hook.maybe_rebuild(force=True, resync_registry=False)
            self._provisioned = True

    async def aclose(self) -> None:
        for port in (self._registry, self._secrets, self._telemetry):
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
        try:
            return await self._router.ask(prompt, operation=operation, trace_id=trace_id, wait=wait)
        except NoLLMAvailableError:
            self._maybe_alert_underprov()
            raise

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
        try:
            return await self._router.chat(
                messages,
                tools=tools,
                operation=operation,
                trace_id=trace_id,
                wait=wait,
            )
        except NoLLMAvailableError:
            self._maybe_alert_underprov()
            raise

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    async def get(self, name: str) -> AsyncLLM:
        await self.ensure_pool()
        return self._pool_view.get(name)

    async def count(self) -> int:
        await self.ensure_pool()
        return self._pool_view.count()

    async def snapshot(self) -> Mapping[str, LLMSnapshot]:
        await self.ensure_pool()
        return await self._pool_view.snapshot()

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
    # Manual disable — the one verdict that actually excludes
    # ------------------------------------------------------------------

    async def disable_llm(self, name: str) -> None:
        """Set the manual latch: withdraws the slot, survives preset rolls, covers
        every operation including future ones. Only ``enable_llm`` clears it."""
        await self.ensure_pool()
        self._pool.set_disabled(name)
        if isinstance(self._base_telemetry, DisabledMapProtocol):
            await self._base_telemetry.set_disabled(name, True)

    async def enable_llm(self, name: str) -> None:
        """Clear the manual latch — a re-enabled model rehabilitates through new
        ratings, no quality reset exists."""
        await self.ensure_pool()
        await self._pool.clear_disabled(name)
        if isinstance(self._base_telemetry, DisabledMapProtocol):
            await self._base_telemetry.set_disabled(name, False)

    # ------------------------------------------------------------------
    # Call journal / retention
    # ------------------------------------------------------------------

    async def calls(self, *, limit: int) -> list[Call]:
        return await self._require_queryable().calls(limit=limit, scope=self._scope)

    async def purge_calls(self, *, before: datetime) -> int:
        return await self._require_queryable().purge_calls(before=before)

    async def alerts(self) -> list[Alert]:
        if self._optimizer is None:
            return []
        return self._optimizer.alerts()

    def _maybe_alert_underprov(self) -> None:
        """Fire when zero keyed configs are routable — the genuine "no usable models" alarm.

        A keyless config is never enqueued/acquired/cooled (see the partial-key framing
        in architecture.md), so it must be excluded here: with even one keyless config
        present, an unfiltered check could never observe "all non-AVAILABLE", masking
        the real alarm even when every *keyed* config is COOLING.
        """
        if self._optimizer is None:
            return
        if not self._pool.configs:
            return
        now = time.monotonic()
        if now - self._last_underprov_alert < self._underprov_alert_interval:
            return
        keyed_names = [name for name in self._pool.configs if self._pool.has_key(name)]
        all_offline = all(
            self._pool.state(name).phase is not LifecyclePhase.AVAILABLE for name in keyed_names
        )
        if all_offline:
            self._last_underprov_alert = now
            self._optimizer.add_alert(
                "pool under-provisioned: all LLMs are COOLING — add more LLMs to the registry",
            )

    def _require_queryable(self) -> QueryableTelemetryProtocol:
        if not isinstance(self._base_telemetry, QueryableTelemetryProtocol):
            raise TypeError(
                "this telemetry backend is not queryable — use a queryable backend"
                " (e.g. llmbroker.sqlite.Telemetry) for calls()/purge_calls()",
            )
        return cast(QueryableTelemetryProtocol, self._telemetry)

"""The ``AsyncBroker`` façade over the LLM pool and its collaborators.

``AsyncBroker`` owns the external ports (registry, secrets, knowledge store),
lazily provisions the live ``LLMPool`` once, and delegates each operation to
the collaborator that owns it:

* ``Catalog``       — pool membership in sync with the registry; ``sync(preset)``
  mirrors a preset into it (the only registry write path)
* ``Router``        — routing a completion over the pool with failover
* ``PoolView``       — read-only views of current pool state
* ``_LearningHook`` — quality windows, dead-key drops, and the debounced
  journal rebuild feeding shared cooldowns, snapshot metrics, and the admin
  disabled-verdict map (only wired when ``optimize`` is truthy)

The call journal (``calls``) is a thin pass-through to a queryable knowledge
backend; each backend self-purges records past its retention horizon. There is
no alerts API: the few human-actionable events (dead key, demotion flip,
under-provisioned pool) are log lines; hosts poll ``snapshot()`` for current
raw state or hook the ``llmbroker`` logger.
"""

import asyncio
import logging
import time
from collections.abc import Mapping
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
    AsyncResourceProtocol,
    Call,
    LifecyclePhase,
    LLMSnapshot,
)
from llmbroker.optimizer import Optimizer
from llmbroker.protocols.backend_stack import BackendStack
from llmbroker.protocols.knowledge import (
    DisabledMapProtocol,
    KnowledgeProtocol,
    QueryableKnowledgeProtocol,
)
from llmbroker.protocols.registry import RegistryProtocol
from llmbroker.protocols.secrets import SecretsProtocol
from llmbroker.standalone.knowledge import FileKnowledge
from llmbroker.standalone.registry import Registry
from llmbroker.standalone.secrets import Secrets, as_secrets

logger = logging.getLogger("llmbroker.broker")


def _default_knowledge(registry: RegistryProtocol) -> KnowledgeProtocol:
    """A file/TOML registry gets a ``state/`` dir sibling to its config file;
    any other registry (a bare DB registry, a custom object) falls back to
    ``./state`` under the CWD — not an error, just an unopinionated default."""
    if isinstance(registry, Registry):
        return FileKnowledge(registry.path.parent / "state")
    return FileKnowledge(Path("state"))


class AsyncBroker:
    """Façade over the LLM pool: route completions, inspect state, edit the catalog."""

    def __init__(  # noqa: PLR0913
        self,
        registry: RegistryProtocol | str | Path | None = None,
        *,
        stack: BackendStack | None = None,
        secrets: SecretsProtocol | None = None,
        knowledge: KnowledgeProtocol | None = None,
        optimize: bool | Optimizer = True,
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
        knowledge = (
            knowledge
            if knowledge is not None
            else (stack.knowledge if stack is not None else _default_knowledge(registry))
        )

        if isinstance(optimize, Optimizer):
            self._optimizer: Optimizer | None = optimize
        elif optimize:
            self._optimizer = Optimizer()
        else:
            self._optimizer = None

        self._registry = registry
        self._secrets = secrets
        self._base_knowledge = knowledge
        self._scope = scope

        pool = LLMPool(optimizer=self._optimizer)
        self._pool = pool
        self._catalog = Catalog(registry, secrets, pool, scope=scope)

        self._learning_hook: _LearningHook | None = None
        effective_knowledge: KnowledgeProtocol
        if self._optimizer is not None:
            self._learning_hook = _LearningHook(
                self._optimizer,
                knowledge,
                pool,
                self._catalog.resync,
            )
            effective_knowledge = self._learning_hook
        else:
            effective_knowledge = knowledge

        self._knowledge = effective_knowledge
        self._router = Router(pool, effective_knowledge, scope=scope, optimizer=self._optimizer)
        self._pool_view = PoolView(pool, effective_knowledge)

        self._provisioned = False
        self._provision_lock = asyncio.Lock()
        self._last_underprov_alert: float = float("-inf")
        self._underprov_alert_interval: float = 60.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def ensure_pool(self) -> None:
        """Lazy idempotent initializer — provisions the pool exactly once.

        Raises if the registry is empty — call ``sync(preset)`` first.
        """
        if self._provisioned:
            return
        async with self._provision_lock:
            if self._provisioned:
                return
            await self._catalog.provision()
            if self._learning_hook is not None:
                # warm start — provision() above already resynced the registry
                await self._learning_hook.maybe_rebuild(force=True, resync_registry=False)
            self._provisioned = True

    async def sync(self, preset: RegistryProtocol | str | Path) -> None:
        """Mirror ``preset`` into the registry: add new entries, update existing
        ones, delete entries absent from the preset. Explicit and idempotent —
        call it once to initialize a fresh DB, or again whenever the preset changes.
        If the pool is already provisioned, the change takes effect immediately.
        """
        if isinstance(preset, (str, Path)):
            preset = Registry(preset)
        await self._catalog.sync(preset)
        if isinstance(self._base_knowledge, DisabledMapProtocol):
            configs = await self._registry.load(user_id=None)
            await self._base_knowledge.seed_disabled([c.name for c in configs])
        if self._provisioned:
            await self._catalog.resync()

    async def aclose(self) -> None:
        for port in (self._registry, self._secrets, self._knowledge):
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
    # Manual disable — the one verdict that actually excludes
    # ------------------------------------------------------------------

    async def disable_llm(self, name: str) -> None:
        """Set the manual latch: withdraws the slot, survives preset rolls, covers
        every operation including future ones. Only ``enable_llm`` clears it."""
        await self.ensure_pool()
        self._pool.set_disabled(name)
        if isinstance(self._base_knowledge, DisabledMapProtocol):
            await self._base_knowledge.set_disabled(name, True)

    async def enable_llm(self, name: str) -> None:
        """Clear the manual latch — a re-enabled model rehabilitates through new
        ratings, no quality reset exists."""
        await self.ensure_pool()
        await self._pool.clear_disabled(name)
        if isinstance(self._base_knowledge, DisabledMapProtocol):
            await self._base_knowledge.set_disabled(name, False)

    # ------------------------------------------------------------------
    # Call journal
    # ------------------------------------------------------------------

    async def calls(self, *, limit: int) -> list[Call]:
        return await self._require_queryable().calls(limit=limit, scope=self._scope)

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
            logger.warning(
                "pool under-provisioned: all LLMs are COOLING — add more LLMs to the registry",
            )

    def _require_queryable(self) -> QueryableKnowledgeProtocol:
        if not isinstance(self._base_knowledge, QueryableKnowledgeProtocol):
            raise TypeError(
                "this knowledge backend is not queryable — use a queryable backend"
                " (e.g. llmbroker.sqlite.Knowledge) for calls()",
            )
        return cast(QueryableKnowledgeProtocol, self._knowledge)

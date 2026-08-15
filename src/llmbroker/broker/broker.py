"""The ``AsyncBroker`` façade: it owns the three ports, provisions the live pool
once, and delegates each operation to the collaborator that owns it."""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import aclosing
from datetime import datetime
from pathlib import Path

from llmbroker.broker.aliases import resolve_declared
from llmbroker.broker.catalog import Catalog
from llmbroker.broker.keyring import KeyRing, known_refs
from llmbroker.broker.learning import TAIL_READ_LIMIT, Learner, metrics_from_calls
from llmbroker.broker.llms import AsyncLLMs
from llmbroker.broker.pool import LLMPool
from llmbroker.broker.pool_view import PoolView
from llmbroker.broker.presets import PresetSource
from llmbroker.broker.refresher import ModelListRefresher
from llmbroker.broker.report import alias_lines
from llmbroker.broker.result import AsyncLLM, AsyncResult
from llmbroker.broker.router import Router
from llmbroker.broker.source import (
    default_secrets,
    default_store,
    resolve_source,
    zero_config_ports,
)
from llmbroker.direct import AsyncDirectClient
from llmbroker.exceptions import (
    NoLLMAvailableError,
    UnknownModelError,
)
from llmbroker.home import home_dir
from llmbroker.models import (
    AsyncResourceProtocol,
    Call,
    DeclaredModels,
    LifecyclePhase,
    LLMConfig,
    LLMMetrics,
    LLMStats,
    PoolSnapshot,
    SyncReport,
)
from llmbroker.optimizer import Optimizer
from llmbroker.protocols.registry import RegistryProtocol
from llmbroker.protocols.secrets import SecretsProtocol
from llmbroker.protocols.store import (
    DisabledMapProtocol,
    QueryableStoreProtocol,
    StoreProtocol,
)
from llmbroker.standalone.secrets import as_secrets

logger = logging.getLogger("llmbroker.broker")

_DEFAULT_STATS_LIMIT = 1000
_DEFAULT_SYNC_INTERVAL = 86_400.0  # seconds
_DEFAULT_SYNC_SOURCE = "freetier"

# Callers are cheap and long-lived processes see many; the cap keeps a per-user
# deployment from holding a ring per user seen since start. Eviction costs a re-read.
_MAX_CALLERS = 1024

# The reactive trigger's floor: a pool that stays exhausted under traffic would
# otherwise ask the ports on every call.
_EXHAUSTION_DEBOUNCE_SEC = 60.0


class _SyncDefault:
    """``sync=`` left unstated — distinct from ``None``, which follows nothing."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<default>"


_SYNC_DEFAULT = _SyncDefault()


def _check_sync_interval(sync_interval: float | None) -> None:
    """Zero is refused rather than read as "never": it makes the clock permanently due,
    so the process would refetch the curated list back to back for as long as it serves."""
    if sync_interval is not None and sync_interval <= 0:
        raise ValueError(
            "sync_interval must be a positive number of seconds, or None to fetch"
            " nothing on this installation's own initiative",
        )


def _resolve_sync(sync: str | None | _SyncDefault, registry: object) -> str | None:
    """What this installation follows, refusing to guess for a registry the host built."""
    if not isinstance(sync, _SyncDefault):
        return sync
    if registry is None or isinstance(registry, (str, Path)):
        return _DEFAULT_SYNC_SOURCE
    raise ValueError(
        "a registry object holds a model list this installation owns, so sync= must say"
        f" what it follows: sync={_DEFAULT_SYNC_SOURCE!r} to keep following the curated"
        " preset, or sync=None to follow nothing",
    )


class AsyncBroker:
    """Façade over the LLM pool: route completions, inspect state, edit the catalog.

    Every constructor argument is documented in ``docs/`` — see "Usage" for the
    source, ``sync`` and ``home``, and "Direct model calls" for ``direct``.
    """

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
        _check_sync_interval(sync_interval)
        source = _resolve_sync(sync, registry)
        self._home = home_dir(home)
        source_secrets: SecretsProtocol | None = None
        source_store: StoreProtocol | None = None
        source_label: str | None = None
        if registry is None:
            registry, source_secrets, source_store = zero_config_ports(self._home)
        elif isinstance(registry, (str, Path)):
            source_label = str(registry)
            registry, source_secrets, source_store = resolve_source(registry)

        secrets = (
            as_secrets(secrets) if secrets is not None else (source_secrets or default_secrets())
        )
        store = store if store is not None else (source_store or default_store())

        if isinstance(optimize, Optimizer):
            self._optimizer: Optimizer | None = optimize
        elif optimize:
            self._optimizer = Optimizer()
        else:
            self._optimizer = None

        self._registry = registry
        self._secrets = secrets
        self._store = store
        self._presets = PresetSource(self._home)
        self._shared_ring = KeyRing(secrets)
        self._rings: dict[str, KeyRing] = {}
        self._known: frozenset[str] | None = None

        self._declared = tuple(direct)
        self._autofetch = sync_interval is not None
        self._last_declared: DeclaredModels | None = None
        pool = LLMPool(optimizer=self._optimizer)
        self._pool = pool
        self._catalog = Catalog(
            registry,
            secrets,
            pool,
            self._shared_ring,
            store,
            overlay=self._resolve_declared if self._declared else None,
            autofill=self._autofetch,
            relearn=self._relearn,
        )

        self._learner: Learner | None = None
        if self._optimizer is not None:
            self._learner = Learner(self._optimizer, store, pool)

        self._router = Router(
            pool,
            store,
            optimizer=self._optimizer,
            learner=self._learner,
        )
        self._pool_view = PoolView(
            pool,
            self._metrics_map,
            lambda: self._catalog.health,
            lambda: self._catalog.direct_missing_keys,
            lambda: self._catalog.payable,
        )

        self._refresher = ModelListRefresher(
            registry,
            self._catalog,
            store,
            self._presets,
            source=source,
            interval=sync_interval,
            home=self._home,
            declared=self._declared,
            target_label=source_label,
            live=lambda: self._provisioned,
            rebuild=self.rebuild,
        )

        self._provisioned = False
        self._provision_lock = asyncio.Lock()
        self._last_underprov_alert: float = float("-inf")
        self._underprov_alert_interval: float = 60.0
        self._next_exhaustion_rebuild: float = float("-inf")
        self.llms = self._caller(self._shared_ring)

    def _caller(self, ring: KeyRing) -> AsyncLLMs:
        return AsyncLLMs(
            ring,
            router=self._router,
            catalog=self._catalog,
            pool_view=self._pool_view,
            store=self._store,
            learner=self._learner,
            ensure_pool=self.ensure_pool,
            on_exhausted=self._on_exhausted,
        )

    def for_scope(self, scope: str) -> AsyncLLMs:
        """A caller that pays with ``scope``\'s own keys, falling back to the shared
        ones, and writes ``scope`` on every row it journals. Costs no I/O."""
        if not scope:
            raise ValueError("scope must not be empty string; use broker.llms for unscoped")
        ring = self._rings.get(scope)
        if ring is None:
            if len(self._rings) >= _MAX_CALLERS:
                self._rings.pop(next(iter(self._rings)))
            ring = KeyRing(
                self._secrets,
                scope=scope,
                shared=self._shared_ring,
                known=self._known,
            )
            self._rings[scope] = ring
        return self._caller(ring)

    async def rebuild(self) -> None:
        """Rebuild the pool: every caller's keys, the registry, pool membership, the
        disabled map and quality, wholesale. Fires on exactly four triggers — start,
        the refresh clock, an explicit ``sync()``, and pool exhaustion."""
        known = await known_refs(self._secrets)
        self._known = known
        # Over a copy: a request may ask for a caller while this is awaiting.
        for ring in (self._shared_ring, *list(self._rings.values())):
            await ring.refresh(known)
        await self._catalog.rebuild(known)

    async def _relearn(self) -> None:
        if self._learner is not None:
            await self._learner.relearn()

    async def _rebuild_safely(self, reason: str) -> None:
        """A rebuild reached from a caller's own call must never fail it: an
        unreadable port leaves the pool exactly as it is, and says so."""
        try:
            await self.rebuild()
        except Exception:  # noqa: BLE001 - a background re-read may not break a request
            logger.exception("pool rebuild on %s failed, continuing on the current pool", reason)

    async def _metrics_map(self) -> dict[str, LLMMetrics]:
        """Per-LLM metrics from whatever is available: the learner's cache, a
        queryable store's tail, or nothing."""
        if self._learner is not None:
            return self._learner.metrics
        if isinstance(self._store, QueryableStoreProtocol):
            return metrics_from_calls(await self._store.calls(limit=TAIL_READ_LIMIT))
        return {}

    async def _resolve_declared(self) -> DeclaredModels:
        """Re-resolve ``direct=``, keeping the resolution already in use when the
        catalog cannot be read or no longer carries an alias. Only the first
        resolution raises — see ``rules/direct-by-name.md``."""
        previous = self._last_declared
        try:
            resolved, moved = await resolve_declared(
                self._declared,
                self._presets,
                previous=previous,
                fetch=self._autofetch,
            )
        except (UnknownModelError, ValueError, OSError) as exc:
            if previous is None:
                raise
            logger.warning(
                "direct= could not be re-resolved (%s) — declared models stay on the"
                " resolution already in use",
                exc,
            )
            return previous
        for line in alias_lines(moved):
            logger.info("direct=: %s", line)
        self._last_declared = resolved
        return resolved

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def ensure_pool(self) -> None:
        """Lazy idempotent initializer — provisions the pool exactly once, and
        schedules the model list refresh when its interval has elapsed.

        Raises if the registry is empty and nothing filled it — sync a model list in.
        """
        if not self._provisioned:
            async with self._provision_lock:
                if not self._provisioned:
                    await self._refresher.before_provision()
                    await self.rebuild()
                    self._catalog.check_not_empty()
                    self._provisioned = True
        # Outside the lock: a refresh calls sync(), and inside it the catalog is
        # mid-provision.
        self._refresher.schedule()

    @property
    def last_sync_report(self) -> SyncReport | None:
        """What the last sync — explicit or refreshed — did, or ``None`` if none has
        run. A host forwards it to its own admin channel."""
        return self._refresher.last_report

    async def sync(self, source: str | None = None) -> SyncReport | None:
        """Merge the curated preset named by ``source`` into the registry and return
        what it did; with no argument, whatever this installation follows — the paid
        catalog alone has no report. See ``rules/model-list.md``."""
        return await self._refresher.sync(source)

    async def aclose(self) -> None:
        # Before the ports: a refresh in flight would otherwise write through a
        # registry whose driver is closing.
        await self._refresher.aclose()
        await self._router.aclose()
        for port in (self._registry, self._secrets, self._store):
            if isinstance(port, AsyncResourceProtocol):
                await port.aclose()

    async def __aenter__(self) -> "AsyncBroker":
        await self.ensure_pool()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ------------------------------------------------------------------
    # Routing — delegated to the unscoped caller
    # ------------------------------------------------------------------

    async def ask(
        self,
        prompt: str,
        *,
        operation: str | None = None,
        trace_id: str | None = None,
        wait: float | None = None,
    ) -> AsyncResult:
        return await self.llms.ask(prompt, operation=operation, trace_id=trace_id, wait=wait)

    async def chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        operation: str | None = None,
        trace_id: str | None = None,
        wait: float | None = None,
    ) -> AsyncResult:
        return await self.llms.chat(
            messages,
            tools=tools,
            operation=operation,
            trace_id=trace_id,
            wait=wait,
        )

    async def stream(
        self,
        prompt: str,
        *,
        operation: str | None = None,
        trace_id: str | None = None,
        wait: float | None = None,
    ) -> AsyncIterator[str]:
        """Route a completion over the pool and yield text deltas as they arrive.
        Fails over like ``ask`` until the first delta, then raises
        ``StreamInterruptedError``. Async-only."""
        async with aclosing(
            self.llms.stream(prompt, operation=operation, trace_id=trace_id, wait=wait),
        ) as deltas:
            async for delta in deltas:
                yield delta

    async def direct(
        self,
        alias: str | None = None,
        *,
        name: str | None = None,
    ) -> AsyncDirectClient:
        """A client for exactly one model of your own — no pool, no failover.

        Takes exactly one of ``alias`` or ``name=``; raises ``PoolModelError``,
        ``UnknownModelError`` or ``MissingKeyError``. See ``docs/`` "Direct model calls".
        """
        return await self.llms.direct(alias, name=name)

    async def record_quality(
        self,
        llm_name: str,
        operation: str | None,
        score: float,
        *,
        call_id: str | None = None,
    ) -> None:
        """Record a quality score for a past call — the delayed counterpart of
        ``result.record_quality``. The host supplies the rating identity, so the
        rated call need not still be in the journal."""
        await self.llms.record_quality(llm_name, operation, score, call_id=call_id)

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    async def get(self, name: str) -> AsyncLLM:
        return await self.llms.get(name)

    async def count(self) -> int:
        return await self.llms.count()

    async def snapshot(self) -> PoolSnapshot:
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
        if isinstance(self._store, DisabledMapProtocol):
            await self._store.set_disabled(name, True)

    async def enable_llm(self, name: str) -> None:
        """Clear the manual latch — a re-enabled model rehabilitates through new
        ratings, no quality reset exists."""
        await self.ensure_pool()
        await self._pool.clear_disabled(name)
        if isinstance(self._store, DisabledMapProtocol):
            await self._store.set_disabled(name, False)

    # ------------------------------------------------------------------
    # Call journal
    # ------------------------------------------------------------------

    async def calls(  # noqa: PLR0913 - one narrowing dimension per parameter
        self,
        *,
        limit: int,
        since: datetime | None = None,
        kind: str | None = None,
        operation: str | None = None,
        trace_id: str | None = None,
        call_id: str | None = None,
    ) -> list[Call]:
        """Newest-first journal tail for the whole installation — one caller's own rows
        are ``for_scope(...).calls(...)``. ``since`` bounds ``called_at`` inclusively;
        ``call_id`` matches a call row's own id. Never provisions the pool."""
        return await self.llms.calls(
            limit=limit,
            since=since,
            kind=kind,
            operation=operation,
            trace_id=trace_id,
            call_id=call_id,
        )

    async def stats(
        self,
        *,
        since: datetime | None = None,
        limit: int = _DEFAULT_STATS_LIMIT,
        operation: str | None = None,
    ) -> Mapping[str, LLMStats]:
        """Per-model counts of call records over a window, keyed by model name.
        ``limit`` caps rows read, not the window: totals summing to it mean the
        window may be truncated. Never provisions the pool."""
        return await self.llms.stats(since=since, limit=limit, operation=operation)

    async def _on_exhausted(self, exc: NoLLMAvailableError, ring: KeyRing) -> bool:
        """The reactive trigger: a pool that could not answer is re-read, debounced and
        skipped for a caller already holding every key. Answers whether the re-read
        actually ran, which is what makes the caller's second pass worth taking."""
        self._maybe_alert_underprov(exc)
        now = time.monotonic()
        if now < self._next_exhaustion_rebuild:
            return False
        if await self._fully_keyed(ring):
            return False
        self._next_exhaustion_rebuild = now + _EXHAUSTION_DEBOUNCE_SEC
        await self._rebuild_safely("pool exhaustion")
        return True

    async def _fully_keyed(self, ring: KeyRing) -> bool:
        """Whether this caller can already pay for every ref the pool names. An empty
        pool is not a full set: there the registry itself is what needs re-reading."""
        refs = {cfg.api_key_ref for cfg in self._pool.configs.values() if cfg.api_key_ref}
        return bool(refs) and refs <= await ring.payable(refs)

    def _maybe_alert_underprov(self, exc: NoLLMAvailableError) -> None:
        """Fire when zero *keyed* configs are routable — the genuine alarm.

        Keyless configs are excluded because they are never cooled, so one of them
        would mask "every keyed model is COOLING"; other reasons log their own line.
        """
        if exc.reason != "timeout":
            return
        if self._optimizer is None:
            return
        if not self._pool.configs:
            return
        now = time.monotonic()
        if now - self._last_underprov_alert < self._underprov_alert_interval:
            return
        payable = self._catalog.payable
        keyed_names = [
            name for name, cfg in self._pool.configs.items() if cfg.api_key_ref in payable
        ]
        all_offline = all(
            self._pool.state(name).phase is not LifecyclePhase.AVAILABLE for name in keyed_names
        )
        if all_offline:
            self._last_underprov_alert = now
            logger.warning(
                "pool under-provisioned: all LLMs are COOLING — add more LLMs to the registry",
            )

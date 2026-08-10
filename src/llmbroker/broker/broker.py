"""The ``AsyncBroker`` façade: it owns the three ports, provisions the live pool
once, and delegates each operation to the collaborator that owns it."""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import aclosing
from datetime import datetime
from pathlib import Path

import httpx

from llmbroker.broker.aliases import resolve_declared
from llmbroker.broker.catalog import Catalog, find_declared, resolve_key
from llmbroker.broker.keys import KeyProbe
from llmbroker.broker.learning import TAIL_READ_LIMIT, Learner, metrics_from_calls
from llmbroker.broker.pool import LLMPool
from llmbroker.broker.pool_view import PoolView
from llmbroker.broker.presets import PresetSource
from llmbroker.broker.refresher import LineupRefresher
from llmbroker.broker.report import alias_lines
from llmbroker.broker.result import AsyncLLM, AsyncResult
from llmbroker.broker.router import Router
from llmbroker.broker.source import (
    default_secrets,
    default_store,
    resolve_source,
    zero_config_ports,
)
from llmbroker.broker.stats import stats_from_calls
from llmbroker.chat import make_client
from llmbroker.direct import AsyncDirectClient
from llmbroker.exceptions import (
    MissingKeyError,
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
    check_limit,
    check_score,
    to_utc,
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


class _SyncDefault:
    """``sync=`` left unstated — distinct from ``None``, which follows nothing."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<default>"


_SYNC_DEFAULT = _SyncDefault()


def _check_broker_args(scope: str | None, sync_interval: float | None) -> None:
    if scope == "":
        raise ValueError("scope must not be empty string; use None for unscoped")
    if sync_interval is not None and sync_interval < 0:
        raise ValueError("sync_interval must not be negative")


def _resolve_sync(sync: str | None | _SyncDefault, registry: object) -> str | None:
    """What this installation follows, refusing to guess for a registry the host built."""
    if not isinstance(sync, _SyncDefault):
        return sync
    if registry is None or isinstance(registry, (str, Path)):
        return _DEFAULT_SYNC_SOURCE
    raise ValueError(
        "a registry object holds a lineup this installation owns, so sync= must say"
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
        scope: str | None = None,
        have_keys: bool | Sequence[str] = False,
        sync: str | None | _SyncDefault = _SYNC_DEFAULT,
        sync_interval: float | None = _DEFAULT_SYNC_INTERVAL,
        home: str | Path | None = None,
        direct: Sequence[str | LLMConfig] = (),
    ) -> None:
        _check_broker_args(scope, sync_interval)
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
        self._scope = scope
        self._probe = KeyProbe(secrets, scope=scope, have_keys=have_keys)
        self._presets = PresetSource(self._home)

        self._declared = tuple(direct)
        self._autofetch = sync_interval is not None
        self._last_declared: DeclaredModels | None = None
        pool = LLMPool(optimizer=self._optimizer)
        self._pool = pool
        self._catalog = Catalog(
            registry,
            secrets,
            pool,
            scope=scope,
            overlay=self._resolve_declared if self._declared else None,
            autofill=self._autofetch,
        )

        self._learner: Learner | None = None
        if self._optimizer is not None:
            self._learner = Learner(self._optimizer, store, pool, self._catalog.resync)

        self._router = Router(
            pool,
            store,
            scope=scope,
            optimizer=self._optimizer,
            learner=self._learner,
        )
        self._pool_view = PoolView(
            pool,
            self._metrics_map,
            lambda: self._catalog.health,
            lambda: self._catalog.direct_missing_keys,
        )

        self._refresher = LineupRefresher(
            registry,
            self._catalog,
            store,
            self._probe,
            self._presets,
            source=source,
            interval=sync_interval,
            home=self._home,
            declared=self._declared,
            target_label=source_label,
            live=lambda: self._provisioned,
        )

        self._provisioned = False
        self._provision_lock = asyncio.Lock()
        self._last_underprov_alert: float = float("-inf")
        self._underprov_alert_interval: float = 60.0
        self._direct_http: httpx.AsyncClient | None = None

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
        resolution raises — see ``rules/direct-aliases.md``."""
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
        schedules the lineup refresh when its interval has elapsed.

        Raises if the registry is empty and nothing filled it — sync a lineup in.
        """
        if not self._provisioned:
            async with self._provision_lock:
                if not self._provisioned:
                    await self._refresher.before_provision()
                    await self._catalog.provision()
                    if self._learner is not None:
                        # warm start — provision() above already resynced the registry
                        await self._learner.maybe_rebuild(
                            force=True,
                            resync_registry=False,
                        )
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
        catalog alone has no report. See ``rules/sync-merge.md``."""
        return await self._refresher.sync(source)

    async def aclose(self) -> None:
        # Before the ports: a refresh in flight would otherwise write through a
        # registry whose driver is closing.
        await self._refresher.aclose()
        await self._router.aclose()
        if self._direct_http is not None:
            await self._direct_http.aclose()
            self._direct_http = None
        for port in (self._registry, self._secrets, self._store):
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
        except NoLLMAvailableError as exc:
            self._maybe_alert_underprov(exc)
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
        except NoLLMAvailableError as exc:
            self._maybe_alert_underprov(exc)
            raise

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
        await self.ensure_pool()
        try:
            async with aclosing(
                self._router.stream(
                    [{"role": "user", "content": prompt}],
                    operation=operation,
                    trace_id=trace_id,
                    wait=wait,
                ),
            ) as deltas:
                async for delta in deltas:
                    yield delta
        except NoLLMAvailableError as exc:
            self._maybe_alert_underprov(exc)
            raise

    # ------------------------------------------------------------------
    # Direct single-model access (no pool, no failover)
    # ------------------------------------------------------------------

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
        cfg, key = await self._resolve_direct(alias, name=name)
        if self._direct_http is None:
            self._direct_http = make_client()
        return AsyncDirectClient(
            base_url=cfg.base_url,
            model=cfg.model,
            api_key=key,
            client=self._direct_http,
        )

    async def _resolve_direct(
        self,
        alias: str | None = None,
        *,
        name: str | None = None,
    ) -> tuple[LLMConfig, str]:
        """Look the entry up in its keyspace and resolve its key (shared by the sync façade)."""
        if (alias is None) == (name is None):
            raise ValueError(
                "direct() takes exactly one of alias (positional) or name= —"
                " they are separate keyspaces",
            )
        stored, declared = await self._catalog.entries()
        cfg = find_declared(stored, declared, alias, name)
        ref = alias if alias is not None else name
        key = await resolve_key(self._secrets, cfg.api_key_ref, self._scope)
        if key is None:
            hint = self._catalog.key_help(cfg.api_key_ref)
            raise MissingKeyError(
                f"api_key_ref {cfg.api_key_ref!r} for model {ref!r} could not be resolved"
                " — set the env var or configure a secrets backend" + (f". {hint}" if hint else ""),
            )
        return cfg, key

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
        check_score(score)
        await self.ensure_pool()
        await self._store.record_quality(
            llm_name,
            operation,
            score,
            call_id=call_id,
            scope=self._scope,
        )
        if self._learner is not None:
            self._learner.record_quality_observed(llm_name, operation, score)

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    async def get(self, name: str) -> AsyncLLM:
        await self.ensure_pool()
        return self._pool_view.get(name)

    async def count(self) -> int:
        await self.ensure_pool()
        return self._pool_view.count()

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

    async def calls(
        self,
        *,
        limit: int,
        since: datetime | None = None,
        kind: str | None = None,
        operation: str | None = None,
    ) -> list[Call]:
        """Newest-first journal tail, narrowed by any of ``since`` (inclusive
        ``called_at`` bound), ``kind`` (``"call"`` or ``"quality"``) and ``operation``.

        Never provisions the pool — see ``stats``.
        """
        check_limit(limit)
        return await self._require_queryable().calls(
            limit=limit,
            scope=self._scope,
            since=to_utc(since, "since") if since is not None else None,
            kind=kind,
            operation=operation,
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
        rows = await self.calls(limit=limit, since=since, kind="call", operation=operation)
        return stats_from_calls(rows)

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
        keyed_names = [name for name in self._pool.configs if self._pool.has_key(name)]
        all_offline = all(
            self._pool.state(name).phase is not LifecyclePhase.AVAILABLE for name in keyed_names
        )
        if all_offline:
            self._last_underprov_alert = now
            logger.warning(
                "pool under-provisioned: all LLMs are COOLING — add more LLMs to the registry",
            )

    def _require_queryable(self) -> QueryableStoreProtocol:
        store = self._store
        if not isinstance(store, QueryableStoreProtocol):
            raise TypeError(
                "this store backend is not queryable — use a queryable backend"
                " (e.g. llmbroker.sqlite.Store) for calls()",
            )
        return store

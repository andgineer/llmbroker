"""The ``AsyncBroker`` façade over the LLM pool and its collaborators.

``AsyncBroker`` owns the external ports (registry, secrets, store),
lazily provisions the live ``LLMPool`` once, and delegates each operation to
the collaborator that owns it:

* ``Catalog``       — pool membership in sync with the registry; ``apply``
  writes a merged lineup into it (the only registry write path)
* ``Router``        — routing a completion over the pool with failover
* ``PoolView``       — read-only views of current pool state
* ``Learner``       — quality windows, dead-key drops, and the debounced
  journal rebuild feeding shared cooldowns, snapshot metrics, and the admin
  disabled-verdict map (only wired when ``optimize`` is truthy)
* ``LineupRefresher`` — the lineup this installation follows: when to look
  upstream, and the merge that applies what arrived (``sync`` delegates to it)

The call journal (``calls``) is a thin pass-through to a queryable store
backend; each backend self-purges records past its retention horizon. There is
no alerts API: the few human-actionable events (dead key, demotion flip,
under-provisioned pool) are log lines; hosts poll ``snapshot()`` for current
raw state or hook the ``llmbroker`` logger.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import aclosing
from datetime import datetime
from pathlib import Path

import httpx

from llmbroker.broker.aliases import resolve_declared
from llmbroker.broker.catalog import Catalog, find_custom, resolve_key
from llmbroker.broker.keys import KeyProbe
from llmbroker.broker.learning import TAIL_READ_LIMIT, Learner, metrics_from_calls
from llmbroker.broker.pool import LLMPool
from llmbroker.broker.pool_view import PoolView
from llmbroker.broker.presets import PresetSource
from llmbroker.broker.refresher import LineupRefresher
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


def _check_broker_args(scope: str | None, sync_interval: float) -> None:
    if scope == "":
        raise ValueError("scope must not be empty string; use None for unscoped")
    if sync_interval < 0:
        raise ValueError("sync_interval must not be negative")


class AsyncBroker:
    """Façade over the LLM pool: route completions, inspect state, edit the catalog.

    ``registry`` is the data source, and *no* source is a source: with none given
    the broker runs the curated free pool, resolving keys from the environment and
    keeping its lineup and journal in the home directory.

    ``direct`` declares paid models in two forms and no others: a paid-catalog
    alias (``"opus"``), whose version llmbroker tracks, or a full ``LLMConfig``,
    whose version the caller tracks. They are reached with ``broker.direct(...)``
    and are never routed by the pool. Nothing is written for them — they are
    re-resolved at every provision, which is what keeps an alias current.

    ``have_keys`` declares refs this installation has a key for but the broker
    cannot probe — per-user keys behind ``scope``, a secret injected only in
    production. It is a promise, and it counts only when a sync weighs whether an
    entry is still callable here: it never makes a model routable.

    ``sync`` names the curated preset this installation follows — ``"freetier"`` by
    default — and it is kept current on the ``sync_interval`` clock: a time gate
    decides whether to go to the network at all, an identity gate decides whether
    what arrived changes anything. The check is lazy on activity, so an idle broker
    performs no I/O; it never raises, and its outcome is on ``last_sync_report``.
    ``sync=None`` follows nothing, for a registry filled by other means.

    ``home`` overrides where this broker keeps what llmbroker caches on its own;
    two brokers given different ones share nothing.
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
        sync: str | None = _DEFAULT_SYNC_SOURCE,
        sync_interval: float = _DEFAULT_SYNC_INTERVAL,
        home: str | Path | None = None,
        direct: Sequence[str | LLMConfig] = (),
    ) -> None:
        _check_broker_args(scope, sync_interval)
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
        self._last_declared: DeclaredModels | None = None
        pool = LLMPool(optimizer=self._optimizer)
        self._pool = pool
        self._catalog = Catalog(
            registry,
            secrets,
            pool,
            scope=scope,
            overlay=self._resolve_declared if self._declared else None,
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
            source=sync,
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
        catalog cannot be read or no longer carries an alias.

        The first resolution raises — a typo must be loud at start-up, and there is
        nothing to fall back to. Every later one is a refresh, and a refresh that
        cannot see upstream has nothing to say about where an alias points: the
        same rule the stored ``[[custom]]`` half already follows. Without it a
        catalog that dropped an alias, or a fetch that failed where nothing is
        writable, would break calls the previous resolution was serving fine.
        """
        previous = self._last_declared
        try:
            resolved = await resolve_declared(
                self._declared,
                self._presets,
                floor=previous is None,
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
        # Outside the lock on both paths: a refresh calls sync(), which touches the
        # catalog, and inside the lock the catalog is mid-provision.
        self._refresher.schedule()

    @property
    def last_sync_report(self) -> SyncReport | None:
        """What the last sync — explicit or refreshed — did, or ``None`` if none has
        run. A host forwards it to its own admin channel."""
        return self._refresher.last_report

    async def sync(self, source: str) -> SyncReport:
        """Merge a curated lineup into the registry and return what it did.

        ``source`` is a curated preset name (``"freetier"``), fetching which is the
        only networked operation in the library. An entry the lineup
        drops is removed only when the same provider replaces it, when no key for
        it exists here, or when this installation's journal proves it dead — so a
        sync can never shrink the set of models this installation can actually
        call. Explicit and idempotent: a merge whose result equals what is already
        there writes nothing, applies nothing and logs at DEBUG. If the pool is
        already provisioned a real change takes effect immediately.
        """
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

        Fails over between models exactly like ``ask`` until the first delta;
        after it the answer is already partly the caller's, so a death raises
        ``StreamInterruptedError``. Async-only — the sync ``Broker`` has no
        counterpart.
        """
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
        """Return a direct client for one of your own ``[[custom]]`` models.

        Bypasses the pool and its failover entirely: the returned client calls
        exactly this model and can stream. Pass exactly one of ``alias`` — the
        eternal handle a catalog refresh re-points at the successor version — or
        ``name=``, which pins the exact version and so fails loudly once a
        refresh has moved on. Raises ``PoolModelError`` for a preset-managed pool
        model, ``UnknownModelError`` if nothing matches, ``MissingKeyError`` if
        the key is unset.
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
        configs = await self._catalog.entries()
        cfg = find_custom(configs, alias, name)
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

        ``limit`` caps rows read, guarding against an anomalous window (a retry
        storm); it is not the window itself. When the totals sum to ``limit`` the
        window may be truncated, and ``first_at`` is then the oldest row *read*
        rather than the oldest in the window. Never provisions the pool.
        """
        rows = await self.calls(limit=limit, since=since, kind="call", operation=operation)
        return stats_from_calls(rows)

    def _maybe_alert_underprov(self, exc: NoLLMAvailableError) -> None:
        """Fire when zero keyed configs are routable — the genuine "no usable models" alarm.

        A keyless config is never enqueued/acquired/cooled (see the partial-key framing
        in the key-help rules), so it must be excluded here: with even one keyless config
        present, an unfiltered check could never observe "all non-AVAILABLE", masking
        the real alarm even when every *keyed* config is COOLING. Only a ``"timeout"``
        reason means the pool is merely temporarily exhausted; every other reason
        (no keys, all disabled, empty pool) already logs its own actionable line.
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

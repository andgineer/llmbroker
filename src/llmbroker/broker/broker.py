"""The ``AsyncBroker`` façade over the LLM pool and its collaborators.

``AsyncBroker`` owns the external ports (registry, secrets, store),
lazily provisions the live ``LLMPool`` once, and delegates each operation to
the collaborator that owns it:

* ``Catalog``       — pool membership in sync with the registry; ``apply``
  writes a merged lineup into it (the only registry write path)
* ``Router``        — routing a completion over the pool with failover
* ``PoolView``       — read-only views of current pool state
* ``_LearningHook`` — quality windows, dead-key drops, and the debounced
  journal rebuild feeding shared cooldowns, snapshot metrics, and the admin
  disabled-verdict map (only wired when ``optimize`` is truthy)

The call journal (``calls``) is a thin pass-through to a queryable store
backend; each backend self-purges records past its retention horizon. There is
no alerts API: the few human-actionable events (dead key, demotion flip,
under-provisioned pool) are log lines; hosts poll ``snapshot()`` for current
raw state or hook the ``llmbroker`` logger.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import aclosing
from datetime import datetime
from pathlib import Path

import httpx

from llmbroker.broker.catalog import Catalog, resolve_key
from llmbroker.broker.learning import _LearningHook
from llmbroker.broker.pool import LLMPool
from llmbroker.broker.pool_view import PoolView
from llmbroker.broker.result import AsyncLLM, AsyncResult
from llmbroker.broker.router import Router
from llmbroker.broker.source import resolve_source
from llmbroker.broker.stamps import stamp_age, write_stamp
from llmbroker.broker.stats import stats_from_calls
from llmbroker.broker.upstream import (
    SyncSource,
    check_not_emptying,
    dead_entries,
    keys_are_visible,
    load_sync_source,
    merge_upstream,
    paid_catalog_text,
    present_refs,
    retirement_candidates,
    sync_file,
)
from llmbroker.chat import make_client
from llmbroker.direct import AsyncDirectClient
from llmbroker.exceptions import (
    MissingKeyError,
    NoLLMAvailableError,
    PoolModelError,
    SyncRefusedError,
    UnknownModelError,
)
from llmbroker.home import home_dir
from llmbroker.models import (
    AsyncResourceProtocol,
    Call,
    LifecyclePhase,
    LLMConfig,
    LLMStats,
    PoolSnapshot,
    Retirement,
    SyncReport,
    check_limit,
    check_score,
    to_utc,
)
from llmbroker.optimizer import Optimizer
from llmbroker.protocols.registry import KeyInfoProtocol, RegistryProtocol
from llmbroker.protocols.secrets import SecretsProtocol
from llmbroker.protocols.store import (
    DisabledMapProtocol,
    QueryableStoreProtocol,
    StoreProtocol,
)
from llmbroker.standalone.registry import Registry
from llmbroker.standalone.secrets import Secrets, as_secrets
from llmbroker.standalone.store import FileStore

logger = logging.getLogger("llmbroker.broker")

_DEFAULT_STATS_LIMIT = 1000
_DEFAULT_SYNC_INTERVAL = 86_400.0  # seconds
_DEFAULT_SYNC_SOURCE = "freetier"


def _default_secrets(registry: RegistryProtocol) -> SecretsProtocol:
    """A file/TOML registry resolves keys from the environment with its own
    sibling ``.env`` as fallback — the file ``llmbroker env`` writes. Any other
    registry gets the plain environment resolver."""
    if isinstance(registry, Registry):
        return Secrets(registry.path.parent / ".env")
    return Secrets()


def _default_store(registry: RegistryProtocol) -> StoreProtocol:
    """A file/TOML registry gets a ``store/`` dir sibling to its config file;
    any other registry (a bare DB registry, a custom object) falls back to
    ``./store`` under the CWD — not an error, just an unopinionated default."""
    if isinstance(registry, Registry):
        return FileStore(registry.path.parent / "store")
    return FileStore(Path("store"))


def _file_target_path(registry: Registry) -> Path:
    """A file registry is rewritten as TOML, so only a ``.toml`` config can be synced."""
    if registry.path.suffix.lower() != ".toml":
        raise ValueError(
            f"registry {registry.path} cannot be synced: a file registry is rewritten as"
            " TOML, so only a .toml config is a sync target — convert it, or move the"
            " lineup into a database registry",
        )
    return registry.path


_POOL_MODEL_HINT = (
    "pool models are anonymous — reach them with ask()/chat()/stream(), which route and"
    " learn; add a [[custom]] entry for the model if you need to call it by name"
)


def _find_custom(configs: list[LLMConfig], alias: str | None, name: str | None) -> LLMConfig:
    """Resolve one custom entry from exactly one of the two keyspaces.

    A miss whose string exists in the *other* keyspace says so, since the two are
    one typo apart at a call site.
    """
    if alias is not None:
        for cfg in configs:
            if cfg.custom and cfg.alias == alias:
                return cfg
        if any(c.custom and c.name == alias for c in configs):
            raise UnknownModelError(
                f"no entry with alias {alias!r}; an entry with this name exists"
                f" — call direct(name={alias!r})",
            )
        if any(c.name == alias for c in configs):
            # The pre-alias call shape: direct() took a name, and a pool name at that.
            # Sending it to direct(name=...) first would only spend an error saying so.
            raise PoolModelError(f"{alias!r} is a preset-managed pool model: {_POOL_MODEL_HINT}")
        raise UnknownModelError(f"no entry with alias {alias!r} in the registry")
    for cfg in configs:
        if cfg.custom and cfg.name == name:
            return cfg
    if any(c.custom and c.alias == name for c in configs):
        raise UnknownModelError(
            f"no entry named {name!r}; an entry with this alias exists — call direct({name!r})",
        )
    if any(c.name == name for c in configs):
        raise PoolModelError(f"{name!r} is a preset-managed pool model: {_POOL_MODEL_HINT}")
    raise UnknownModelError(f"no model named {name!r} in the registry")


def _check_broker_args(
    scope: str | None,
    sync_interval: float,
    registry: RegistryProtocol | str | Path | None,
) -> None:
    if scope == "":
        raise ValueError("scope must not be empty string; use None for unscoped")
    if sync_interval < 0:
        raise ValueError("sync_interval must not be negative")
    if registry is None:
        raise ValueError("AsyncBroker requires a `registry` source")


class AsyncBroker:
    """Façade over the LLM pool: route completions, inspect state, edit the catalog.

    ``have_keys`` declares refs this installation has a key for but the broker
    cannot probe — per-user keys behind ``scope``, a secret injected only in
    production. It is a promise, and it counts only when a sync weighs whether an
    entry is still callable here: it never makes a model routable.

    ``sync`` names the lineup this installation follows — the curated preset by
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
        sync: str | Path | None = _DEFAULT_SYNC_SOURCE,
        sync_interval: float = _DEFAULT_SYNC_INTERVAL,
        home: str | Path | None = None,
    ) -> None:
        _check_broker_args(scope, sync_interval, registry)
        source_secrets: SecretsProtocol | None = None
        source_store: StoreProtocol | None = None
        source_label: str | None = None
        if isinstance(registry, (str, Path)):
            source_label = str(registry)
            registry, source_secrets, source_store = resolve_source(registry)

        secrets = (
            as_secrets(secrets)
            if secrets is not None
            else (source_secrets or _default_secrets(registry))
        )
        store = store if store is not None else (source_store or _default_store(registry))

        if isinstance(optimize, Optimizer):
            self._optimizer: Optimizer | None = optimize
        elif optimize:
            self._optimizer = Optimizer()
        else:
            self._optimizer = None

        self._registry = registry
        self._secrets = secrets
        self._base_store = store
        self._scope = scope
        self._have_keys = have_keys

        pool = LLMPool(optimizer=self._optimizer)
        self._pool = pool
        self._catalog = Catalog(registry, secrets, pool, scope=scope)

        self._learning_hook: _LearningHook | None = None
        effective_store: StoreProtocol
        if self._optimizer is not None:
            self._learning_hook = _LearningHook(
                self._optimizer,
                store,
                pool,
                self._catalog.resync,
            )
            effective_store = self._learning_hook
        else:
            effective_store = store

        self._store = effective_store
        self._router = Router(pool, effective_store, scope=scope, optimizer=self._optimizer)
        self._pool_view = PoolView(pool, effective_store, lambda: self._catalog.health)

        self._home = home_dir(home)
        self._source_label = source_label
        self._sync_source = sync
        self._sync_interval = sync_interval
        self._sync_attempted = False
        # Monotonic deadline for the next check; inf until the first one lands.
        self._next_refresh = float("inf")
        self._refresh_task: asyncio.Task[None] | None = None
        self.last_sync_report: SyncReport | None = None

        self._provisioned = False
        self._provision_lock = asyncio.Lock()
        self._last_underprov_alert: float = float("-inf")
        self._underprov_alert_interval: float = 60.0
        self._direct_http: httpx.AsyncClient | None = None

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
                    await self._sync_on_start()
                    await self._catalog.provision()
                    if self._learning_hook is not None:
                        # warm start — provision() above already resynced the registry
                        await self._learning_hook.maybe_rebuild(
                            force=True,
                            resync_registry=False,
                        )
                    self._provisioned = True
        # Outside the lock on both paths: a refresh calls sync(), which touches the
        # catalog, and inside the lock the catalog is mid-provision.
        self._maybe_schedule_refresh()

    def _maybe_schedule_refresh(self) -> None:
        """Fire a background refresh when the interval has elapsed. Synchronous by
        design: the hot path pays one monotonic comparison and nothing else."""
        if time.monotonic() < self._next_refresh:
            return
        if self._refresh_task is not None and not self._refresh_task.done():
            return
        # The deadline moves before the task exists, so a burst of concurrent calls
        # schedules one refresh rather than one per call.
        self._next_refresh = time.monotonic() + self._sync_interval
        self._refresh_task = asyncio.create_task(self._attempt_sync("refresh"))

    async def _sync_on_start(self) -> None:
        """Decide what the lineup needs before the pool is provisioned.

        An empty registry is filled here, blocking: ``provision()`` on one raises,
        so there is no alternative. A check already on record inside the interval
        needs nothing, and its remainder carries into this process. Otherwise the
        pool is provisioned from what is stored and the refresh runs afterwards, off
        the request path.
        """
        if self._sync_source is None or self._sync_attempted:
            return
        self._sync_attempted = True
        if not await self._registry.load():
            await self._attempt_sync("start")
            return
        age = stamp_age(self._home, self._stamp_key(self._sync_source))
        if age is not None and age < self._sync_interval:
            self._next_refresh = time.monotonic() + (self._sync_interval - age)
            return
        self._next_refresh = 0.0

    async def _attempt_sync(self, reason: str) -> None:
        """Best-effort by construction: a refresh that cannot be fetched or cannot
        be applied logs and leaves the running configuration alone. A process must
        not fail to start, and a request must not fail, over a lineup refresh. The
        explicit ``sync()`` call raises instead — that caller has a plan.
        """
        source = self._sync_source
        if source is None:
            return
        try:
            await self.sync(source)
        except SyncRefusedError as exc:
            self.last_sync_report = exc.report
            logger.warning("sync %s refused, continuing on the current config: %s", reason, exc)
        except (ValueError, OSError) as exc:
            logger.warning("sync %s failed, continuing on the current config: %s", reason, exc)
        finally:
            self._next_refresh = time.monotonic() + self._sync_interval

    def _stamp_key(self, source: RegistryProtocol | str | Path) -> str:
        """What was checked, and for whom. Keyed by both because two projects on one
        machine have two lineups to keep current, and one project's check must not
        gate the other's."""
        label = str(source) if isinstance(source, (str, Path)) else type(source).__name__
        return f"{label} {self._target_identity()}"

    def _target_identity(self) -> str:
        if isinstance(self._registry, Registry):
            return str(self._registry.path.resolve())
        if self._source_label is not None:
            return self._source_label
        # No persistent identity of its own: the home directory is the identity.
        return str(self._home)

    async def sync(self, source: RegistryProtocol | str | Path) -> SyncReport:
        """Merge a lineup into the registry and return what it did.

        ``source`` is a curated preset name (``"freetier"`` — the only networked
        operation in the library), or the path of a config file, or a registry;
        a ``.toml`` file registry takes the preset form only. An entry the lineup
        drops is removed only when the same provider replaces it, when no key for
        it exists here, or when this installation's journal proves it dead — so a
        sync can never shrink the set of models this installation can actually
        call. Explicit and idempotent: a merge whose result equals what is already
        there writes nothing, applies nothing and logs at DEBUG. If the pool is
        already provisioned a real change takes effect immediately.
        """
        src = await load_sync_source(source, cache_dir=self._home)
        if isinstance(self._registry, Registry):
            report, changed = await self._sync_file_target(src, _file_target_path(self._registry))
        else:
            report, changed = await self._sync_registry_target(src)
        if changed and isinstance(self._base_store, DisabledMapProtocol):
            configs = await self._registry.load()
            await self._base_store.seed_disabled([c.name for c in configs])
        # Both land before the resync: a resync that raises must not swallow the
        # record of a change already applied.
        if changed:
            logger.info("%s", report)
        else:
            logger.debug("sync %s: no change", report.source)
        self.last_sync_report = report
        write_stamp(self._home, self._stamp_key(source))
        if changed and self._provisioned:
            await self._catalog.resync()
        return report

    async def _present_refs(self, *lineups: list[LLMConfig]) -> set[str]:
        return await present_refs(
            [c.api_key_ref for lineup in lineups for c in lineup],
            self._secrets,
            scope=self._scope,
            have_keys=self._have_keys,
        )

    def _keys_visible(self, present: set[str]) -> bool:
        return keys_are_visible(present, scope=self._scope, have_keys=self._have_keys)

    async def _dead(
        self,
        new_configs: list[LLMConfig],
        current: list[LLMConfig],
        present: set[str],
    ) -> dict[str, Retirement]:
        candidates = retirement_candidates(
            new_configs,
            current,
            present,
            keys_visible=self._keys_visible(present),
        )
        return await dead_entries(candidates, self._base_store)

    async def _sync_file_target(self, src: SyncSource, target: Path) -> tuple[SyncReport, bool]:
        if not src.preset or src.text is None:
            raise ValueError(
                f"cannot sync {src.label} into the file registry {target}: a .toml registry"
                ' takes a curated preset name only (e.g. broker.sync("freetier")). A file or'
                " registry source syncs into a database registry — the vendored-lockfile"
                " deploy path",
            )
        current = await self._registry.load()
        present = await self._present_refs(src.configs, current)
        outcome = await sync_file(
            src.text,
            target,
            source=src.label,
            secrets=self._secrets,
            scope=self._scope,
            have_keys=self._have_keys,
            dead=await self._dead(src.configs, current, present),
            fetch_catalog=paid_catalog_text,
        )
        # Outside the identity gate: a key that arrived in the environment is
        # bootstrapped by a sync whether or not the lineup itself moved.
        await self._catalog.seed_secrets(list(outcome.configs))
        for notice in outcome.notices:
            logger.info("sync %s: %s", src.label, notice)
        for warning in outcome.warnings:
            logger.warning("sync %s: %s", src.label, warning)
        return outcome.report, outcome.changed

    async def _sync_registry_target(self, src: SyncSource) -> tuple[SyncReport, bool]:
        current = await self._registry.load()
        current_keys = (
            await self._registry.key_info() if isinstance(self._registry, KeyInfoProtocol) else {}
        )
        present = await self._present_refs(src.configs, current)
        merged, _keys, report = merge_upstream(
            src.configs,
            src.keys,
            current,
            current_keys,
            present,
            source=src.label,
            dead=await self._dead(src.configs, current, present),
            keys_visible=self._keys_visible(present),
            keys_scoped=self._scope is not None,
        )
        check_not_emptying(merged, current, report)
        # Keyed by name rather than compared as lists: a database registry hands
        # its rows back ordered by name, so position here is the backend's, not
        # the lineup's, and would report every no-op sync as a change.
        changed = {c.name: c for c in merged} != {c.name: c for c in current}
        if changed:
            await self._catalog.apply(merged)
        else:
            await self._catalog.seed_secrets(merged)
        return report, changed

    async def aclose(self) -> None:
        # Before the ports: a refresh in flight would otherwise write through a
        # registry whose driver is closing.
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._refresh_task
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
        configs = await self._registry.load()
        cfg = _find_custom(configs, alias, name)
        ref = alias if alias is not None else name
        key = await resolve_key(self._secrets, cfg.api_key_ref, self._scope)
        if key is None:
            raise MissingKeyError(
                f"api_key_ref {cfg.api_key_ref!r} for model {ref!r} could not be resolved"
                " — set the env var or configure a secrets backend",
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
        if isinstance(self._base_store, DisabledMapProtocol):
            await self._base_store.set_disabled(name, True)

    async def enable_llm(self, name: str) -> None:
        """Clear the manual latch — a re-enabled model rehabilitates through new
        ratings, no quality reset exists."""
        await self.ensure_pool()
        await self._pool.clear_disabled(name)
        if isinstance(self._base_store, DisabledMapProtocol):
            await self._base_store.set_disabled(name, False)

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
        in architecture.md), so it must be excluded here: with even one keyless config
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
        # The learning hook satisfies the protocol structurally whatever it wraps,
        # so the question can only be put to the backend itself — which is then also
        # what answers the read: the hook adds nothing to it but pass-through.
        store = self._base_store
        if not isinstance(store, QueryableStoreProtocol):
            raise TypeError(
                "this store backend is not queryable — use a queryable backend"
                " (e.g. llmbroker.sqlite.Store) for calls()",
            )
        return store

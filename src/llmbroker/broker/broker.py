"""The ``AsyncBroker`` façade over the LLM pool and its collaborators.

``AsyncBroker`` owns the external ports (registry, secrets, telemetry, state
store), lazily provisions the live ``LLMPool`` once, and delegates each
operation to the collaborator that owns it:

* ``Catalog``  — pool membership in sync with the registry (seed/load + edits)
* ``Router``   — routing a completion over the pool with failover
* ``PoolView`` — read-only views of current pool state
* ``_ProfileSync`` — the learned-profile durability/cluster-sharing path: warm
  start at provision, live delta flush + demotion re-derivation, and the
  durable registry snapshot (debounced, and on ``aclose``)

The call journal (``calls``/``purge_calls``) is a thin pass-through to a queryable
telemetry backend; ``alerts`` is delegated to ``Optimizer``.
"""

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from llmbroker.broker.catalog import Catalog
from llmbroker.broker.pool import LLMPool
from llmbroker.broker.pool_view import PoolView
from llmbroker.broker.result import AsyncLLM, AsyncResult
from llmbroker.broker.router import Router
from llmbroker.exceptions import NoLLMAvailableError
from llmbroker.models import (
    Alert,
    AsyncResourceProtocol,
    Call,
    CallStatus,
    LifecyclePhase,
    LLMConfig,
    LLMProfile,
    LLMSnapshot,
    SeedPolicy,
)
from llmbroker.optimizer import Optimizer, OptimizerPolicy, OptimizerTelemetry
from llmbroker.protocols.registry import RegistryProtocol
from llmbroker.protocols.secrets import SecretsProtocol
from llmbroker.protocols.state_store import StateStoreProtocol
from llmbroker.protocols.telemetry import QueryableTelemetryProtocol, TelemetryProtocol
from llmbroker.standalone.registry import Registry
from llmbroker.standalone.secrets import Secrets, as_secrets
from llmbroker.standalone.telemetry import Telemetry

logger = logging.getLogger("llmbroker.broker")

_REFRESH_TTL = 2.0  # seconds — merged-summary re-read + demotion re-derivation heartbeat
_SNAPSHOT_DEBOUNCE = 30.0  # seconds — durable registry snapshot cadence


class _ProfileSync:
    """Owns the learned-profile lifecycle for one ``AsyncBroker``: warm start at
    provision, live delta flush + demotion re-derivation, and durable snapshots.

    A thin orchestrator over already-built collaborators (registry, state store,
    optimizer, pool) — it holds no learned data itself. ``benched_meta`` is the
    broker's own ``{name: (since, reason)}`` map, shared by reference so both sides
    stay in sync without a second source of truth.
    """

    def __init__(  # noqa: PLR0913
        self,
        registry: RegistryProtocol,
        state_store: StateStoreProtocol | None,
        pool: LLMPool,
        optimizer: Optimizer,
        user_id: int | str | None,
        benched_meta: dict[str, tuple[datetime | None, str | None]],
    ) -> None:
        self._registry = registry
        self._state_store = state_store
        self._pool = pool
        self._optimizer = optimizer
        self._user_id = user_id
        self._benched_meta = benched_meta
        self._next_refresh: float = 0.0
        self._next_snapshot: dict[str, float] = {}
        self._demotion_alert_times: dict[tuple[str, str | None], float] = {}
        self._global_alert_times: dict[str, float] = {}

    async def warm_start(self) -> None:
        """Read persisted profiles, seed the state store, apply latches/deprecated
        markers, and load the merged summaries into the optimizer."""
        profiles = await self._registry.read_profiles(user_id=self._user_id)
        for name, profile in profiles.items():
            if profile.benched:
                self._pool.set_benched(name)
                self._optimizer.set_benched(name)
                self._benched_meta[name] = (profile.benched_since, profile.benched_reason)
            if self._state_store is not None:
                for operation, kinds in profile.stats.items():
                    for kind, summary in kinds.items():
                        await self._state_store.seed_summary(
                            name,
                            operation,
                            kind,
                            summary,
                            self._user_id,
                        )
        if self._state_store is not None:
            await self._load_merged_into_optimizer()
        else:
            for name, profile in profiles.items():
                self._optimizer.load_summaries(name, profile)
        self._refresh_demotions()

    async def _load_merged_into_optimizer(self) -> None:
        assert self._state_store is not None
        merged = await self._state_store.read_summaries(self._user_id)
        by_name: dict[str, LLMProfile] = {}
        for (name, operation, kind), summary in merged.items():
            by_name.setdefault(name, LLMProfile()).stats.setdefault(operation, {})[kind] = summary
        for name, profile in by_name.items():
            self._optimizer.load_summaries(name, profile)

    async def on_call(self, call: Call) -> None:
        """Flush one call's transport(+latency) delta and opportunistically refresh."""
        if self._state_store is not None:
            decay = self._optimizer.transport_decay
            await self._state_store.apply_summary_delta(
                call.llm_name,
                call.operation,
                "transport",
                decay,
                1.0,
                1.0 if call.status == CallStatus.OK else 0.0,
                1.0,
                1,
                self._user_id,
            )
            if call.status == CallStatus.OK and call.latency_ms is not None:
                await self._state_store.apply_summary_delta(
                    call.llm_name,
                    call.operation,
                    "latency",
                    decay,
                    1.0,
                    float(call.latency_ms),
                    1.0,
                    1,
                    self._user_id,
                )
        await self._maybe_refresh()
        await self._maybe_snapshot(call.llm_name)

    async def on_quality(self, name: str, operation: str | None, score: float) -> None:
        """Flush one quality delta and opportunistically refresh."""
        if self._state_store is not None:
            decay = self._optimizer.quality_decay
            await self._state_store.apply_summary_delta(
                name,
                operation,
                "quality",
                decay,
                1.0,
                score,
                1.0,
                1,
                self._user_id,
            )
        await self._maybe_refresh()
        await self._maybe_snapshot(name)

    async def _maybe_refresh(self) -> None:
        now = time.monotonic()
        if now < self._next_refresh:
            return
        self._next_refresh = now + _REFRESH_TTL
        if self._state_store is not None:
            await self._load_merged_into_optimizer()
        self._refresh_demotions()

    def _refresh_demotions(self) -> None:
        now = time.monotonic()
        for name in list(self._pool.configs):
            demotions = self._optimizer.evaluate_demotions(name)
            demoted_ops = frozenset(op for op, bad in demotions.items() if bad)
            globally_demoted = self._optimizer.is_globally_demoted(name)
            self._pool.update_demotions(name, demoted_ops, globally_demoted=globally_demoted)
            self._maybe_alert(name, demoted_ops, globally_demoted, now)

    def _maybe_alert(
        self,
        name: str,
        demoted_ops: frozenset[str | None],
        globally_demoted: bool,  # noqa: FBT001
        now: float,
    ) -> None:
        opt = self._optimizer
        for op in demoted_ops:
            key = (name, op)
            last = self._demotion_alert_times.get(key, float("-inf"))
            if now - last < opt.demotion_realert_interval:
                continue
            self._demotion_alert_times[key] = now
            bound = opt.wilson_bound(name, op)
            bound_str = f"{bound:.3f}" if bound is not None else "n/a"
            boundary = opt.quality_floor - opt.quality_margin
            msg = (
                f"{name}: quality-demoted for operation={op!r} (Wilson upper bound "
                f"{bound_str} < quality_floor {opt.quality_floor}, demote boundary "
                f"{boundary:.3f})"
            )
            opt.add_alert(msg)
            logger.warning(msg)
        if globally_demoted:
            last = self._global_alert_times.get(name, float("-inf"))
            if now - last >= opt.demotion_realert_interval:
                self._global_alert_times[name] = now
                logger.error(
                    "%s: practically useless for every operation it has been measured on",
                    name,
                )

    async def _maybe_snapshot(self, name: str) -> None:
        now = time.monotonic()
        if now < self._next_snapshot.get(name, 0.0):
            return
        self._next_snapshot[name] = now + _SNAPSHOT_DEBOUNCE
        await self.write_snapshot(name)

    async def write_snapshot(self, name: str) -> None:
        """Best-effort durable snapshot — never lets a write failure break the live
        call/scoring path or block the rest of a shutdown/flush sequence."""
        profile = self._optimizer.to_profile(name)
        since, reason = self._benched_meta.get(name, (None, None))
        profile = replace(
            profile,
            benched=self._pool.is_benched(name),
            benched_since=since,
            benched_reason=reason,
        )
        try:
            await self._registry.write_profile(name, profile, self._user_id)
        except KeyError:
            logger.debug("write_snapshot: %s no longer in the registry — nothing to persist", name)
        except Exception:  # noqa: BLE001
            logger.exception("write_snapshot: failed to persist profile for %s", name)

    async def write_all_snapshots(self) -> None:
        """Persist every pool member's learned profile — what makes a short-lived
        process's learned knowledge accumulate across runs."""
        for name in list(self._pool.configs):
            await self.write_snapshot(name)

    async def reset_quality(self, name: str) -> None:
        """Reset this model's quality aggregates everywhere — optimizer, state store,
        registry snapshot — for a clean trial period after a manual re-enable."""
        if self._state_store is not None:
            merged = await self._state_store.read_summaries(self._user_id)
            for (row_name, operation, kind), summary in merged.items():
                if row_name != name or kind != "quality":
                    continue
                await self._state_store.apply_summary_delta(
                    name,
                    operation,
                    kind,
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    -summary.count,
                    self._user_id,
                )
        self._optimizer.reset_quality(name)
        await self.write_snapshot(name)


class _ProfileSyncTelemetry:
    """Wraps ``OptimizerTelemetry``: after every event, flushes its delta to the
    state store (if configured) and drives the profile-sync heartbeat."""

    def __init__(self, inner: OptimizerTelemetry, sync: _ProfileSync) -> None:
        self._inner = inner
        self._sync = sync

    async def record(self, call: Call) -> None:
        await self._inner.record(call)
        await self._sync.on_call(call)

    async def record_quality(self, call_id: str, score: float) -> None:
        resolved = self._inner.peek_call(call_id)
        await self._inner.record_quality(call_id, score)
        if resolved is not None:
            await self._sync.on_quality(resolved[0], resolved[1], score)

    async def metrics(
        self,
        *,
        since: datetime | None = None,
        user_id: int | str | None = None,
    ) -> dict:
        return await self._inner.metrics(since=since, user_id=user_id)

    async def calls(self, *, limit: int, user_id: int | str | None = None) -> list[Call]:
        return await self._inner.calls(limit=limit, user_id=user_id)

    async def purge_calls(self, *, before: datetime) -> int:
        return await self._inner.purge_calls(before=before)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


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
        seed_policy: SeedPolicy = SeedPolicy.SYNC,
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
        self._benched_meta: dict[str, tuple[datetime | None, str | None]] = {}

        pool = LLMPool(state_store, user_id, on_degraded_tier=self._on_degraded_tier)
        self._pool = pool
        self._catalog = Catalog(
            registry,
            secrets,
            pool,
            seed=seed,
            seed_policy=seed_policy,
            user_id=user_id,
        )

        self._profile_sync: _ProfileSync | None = None
        if self._optimizer is not None:
            opt_telemetry = OptimizerTelemetry(self._optimizer, telemetry, pool)
            self._profile_sync = _ProfileSync(
                registry,
                state_store,
                pool,
                self._optimizer,
                user_id,
                self._benched_meta,
            )
            effective_telemetry: TelemetryProtocol = _ProfileSyncTelemetry(
                opt_telemetry,
                self._profile_sync,
            )
            policy = OptimizerPolicy(self._optimizer)
        else:
            effective_telemetry = telemetry
            policy = None

        self._telemetry = effective_telemetry
        self._router = Router(
            pool,
            effective_telemetry,
            user_id=user_id,
            optimizer=self._optimizer,
            policy=policy,
        )
        self._pool_view = PoolView(pool, effective_telemetry, user_id=user_id)

        self._provisioned = False
        self._provision_lock = asyncio.Lock()
        self._last_underprov_alert: float = float("-inf")
        self._underprov_alert_interval: float = 60.0

    def _on_degraded_tier(self, name: str, operation: str | None, tier: int) -> None:
        if self._optimizer is None:
            return
        self._optimizer.add_alert(
            f"pool degraded: serving {name!r} for operation={operation!r} from tier {tier}"
            " (deprecated or quality-demoted) — add more/better LLMs to the registry",
        )

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
            if self._profile_sync is not None:
                await self._profile_sync.warm_start()
            self._provisioned = True

    async def aclose(self) -> None:
        if self._profile_sync is not None:
            await self._profile_sync.write_all_snapshots()
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
        self._benched_meta.pop(name, None)

    # ------------------------------------------------------------------
    # Manual bench — the one verdict that actually excludes
    # ------------------------------------------------------------------

    async def _bench_profile(
        self,
        name: str,
        *,
        benched: bool,
        since: datetime | None,
        reason: str | None,
    ) -> LLMProfile:
        """Build the profile to persist for a bench-latch change, preserving whatever
        learned stats already exist instead of overwriting them with an empty profile."""
        if self._optimizer is not None:
            profile = self._optimizer.to_profile(name)
        else:
            profiles = await self._registry.read_profiles(user_id=self._user_id)
            profile = profiles.get(name, LLMProfile())
        return replace(profile, benched=benched, benched_since=since, benched_reason=reason)

    async def disable_llm(self, name: str, *, reason: str | None = None) -> None:
        """Set the manual latch: withdraws the slot, survives preset rolls, covers
        every operation including future ones. Only ``enable_llm`` clears it."""
        await self.ensure_pool()
        self._pool.set_benched(name)
        if self._optimizer is not None:
            self._optimizer.set_benched(name)
        since = datetime.now(UTC)
        self._benched_meta[name] = (since, reason)
        profile = await self._bench_profile(name, benched=True, since=since, reason=reason)
        await self._registry.write_profile(name, profile, self._user_id)

    async def enable_llm(self, name: str) -> None:
        """Clear the manual latch and reset the model's quality aggregates — a
        clean trial period so stale evidence cannot immediately re-derive a demotion."""
        await self.ensure_pool()
        self._pool.clear_benched(name)
        self._benched_meta.pop(name, None)
        if self._optimizer is not None:
            self._optimizer.clear_benched(name)
        if self._profile_sync is not None:
            await self._profile_sync.reset_quality(name)
        profile = await self._bench_profile(name, benched=False, since=None, reason=None)
        await self._registry.write_profile(name, profile, self._user_id)

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

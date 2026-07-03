"""The live in-memory routing substrate behind ``AsyncBroker``.

One ``asyncio.Queue`` slot per LLM ⇒ at most one in-flight request per LLM.
A slot is withdrawn from the queue while the LLM is cooling after a 429/503 and
re-added when the cooldown expires. The pool also owns the in-memory
``LLMState`` (cooldown + quality counters) and persists it via the state store.

Tiered selection: candidates for an operation partition into tier 0 (normal),
tier 1 (``deprecated`` — curator withdrew the endorsement), tier 2 (a
globally-demoted model's untried operation — new evidence territory), tier 3
(quality-demoted for this specific operation — evidenced bad). ``acquire()``
serves the best non-empty tier; the caller's ``SelectionPolicy`` ranks only
within that tier. ``deprecated``/demotion markers are soft (consulted at
acquire time, never withdraw a slot) — only the manual bench and a missing key
withdraw a slot from the queue outright.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from llmbroker.broker.state import InMemoryState
from llmbroker.models import LifecyclePhase, LLMConfig, LLMState
from llmbroker.optimizer import SelectionPolicy
from llmbroker.protocols.state_store import StateStoreProtocol

logger = logging.getLogger("llmbroker.broker")

_STORE_CACHE_TTL = 2.0  # seconds
_DEGRADED_TIER_ALERT_INTERVAL = 60.0  # seconds

# Tier ordinals — lower is better; acquire() serves the best non-empty tier.
TIER_NORMAL = 0
TIER_DEPRECATED = 1
TIER_DEMOTED_UNTRIED = 2
TIER_DEMOTED_EVIDENCED = 3


class LLMPool:
    """The pool of LLM slots and their live cooldown / quality state."""

    def __init__(
        self,
        state_store: StateStoreProtocol | None,
        user_id: int | str | None,
        *,
        on_degraded_tier: Callable[[str, str | None, int], None] | None = None,
    ) -> None:
        self._queue: asyncio.Queue[LLMConfig] = asyncio.Queue()
        self._configs: dict[str, LLMConfig] = {}
        self._resolved_keys: dict[str, str] = {}
        self._state = InMemoryState()
        self._state_store = state_store
        self._user_id = user_id
        self._store_cache: dict[str, LLMState] | None = None
        self._store_cache_expires: float = 0.0
        self._benched: set[str] = set()
        self._deprecated: set[str] = set()
        self._demoted_operations: dict[str, frozenset[str | None]] = {}
        self._globally_demoted: set[str] = set()
        self._on_degraded_tier = on_degraded_tier
        self._last_degraded_alert: dict[tuple[str, str | None, int], float] = {}

    # ------------------------------------------------------------------
    # Membership / lookup
    # ------------------------------------------------------------------

    def __contains__(self, name: str) -> bool:
        return name in self._configs

    def __len__(self) -> int:
        return len(self._configs)

    @property
    def configs(self) -> dict[str, LLMConfig]:
        return self._configs

    def config(self, name: str) -> LLMConfig:
        return self._configs[name]

    def has_key(self, name: str) -> bool:
        return name in self._resolved_keys

    def resolved_key(self, name: str) -> str:
        return self._resolved_keys[name]

    # ------------------------------------------------------------------
    # Membership mutation
    # ------------------------------------------------------------------

    def add(self, cfg: LLMConfig, key: str | None) -> None:
        """Register/refresh a config. A ``None`` key leaves any prior key intact.

        Enqueues a routable slot only on first insertion with a resolved key, or on
        the keyless→keyed transition of an existing entry — a keyless config stays
        visible in ``configs`` but is never acquired by the router (see the
        partial-key framing in architecture.md). A manually-benched config is never
        enqueued either, regardless of key transitions.
        """
        was_keyed = cfg.name in self._resolved_keys
        is_new = cfg.name not in self._configs
        self._configs[cfg.name] = cfg
        if key is not None:
            self._resolved_keys[cfg.name] = key
        now_keyed = cfg.name in self._resolved_keys
        if now_keyed and (is_new or not was_keyed) and cfg.name not in self._benched:
            self._queue.put_nowait(cfg)

    def drop(self, name: str) -> None:
        """Remove a config and every per-name tier marker, so a later re-add under the
        same name starts clean rather than inheriting stale bench/demotion state."""
        self._configs.pop(name, None)
        self._resolved_keys.pop(name, None)
        self._benched.discard(name)
        self._deprecated.discard(name)
        self._demoted_operations.pop(name, None)
        self._globally_demoted.discard(name)
        for key in [k for k in self._last_degraded_alert if k[0] == name]:
            del self._last_degraded_alert[key]

    # ------------------------------------------------------------------
    # Manual bench (hard exclusion) / deprecated / demotion markers (soft, tiered)
    # ------------------------------------------------------------------

    def set_benched(self, name: str) -> None:
        """Withdraw the slot — the one verdict that actually excludes.

        Drains any currently-queued slot for ``name`` synchronously (no ``await``
        inside the loop, so no other task can interleave) and leaves the latch set
        so ``_reenqueue_config`` refuses to re-admit it later (mid-cooldown or
        released after an in-flight call completes).
        """
        self._benched.add(name)
        drained = []
        while True:
            try:
                drained.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        for cfg in drained:
            if cfg.name != name:
                self._queue.put_nowait(cfg)

    def clear_benched(self, name: str) -> None:
        """Readmit the slot if the config is still known and keyed."""
        self._benched.discard(name)
        if name in self._configs and name in self._resolved_keys:
            self._queue.put_nowait(self._configs[name])

    def is_benched(self, name: str) -> bool:
        return name in self._benched

    def set_deprecated(self, name: str) -> None:
        self._deprecated.add(name)

    def clear_deprecated(self, name: str) -> None:
        self._deprecated.discard(name)

    def is_deprecated(self, name: str) -> bool:
        return name in self._deprecated

    def update_demotions(
        self,
        name: str,
        demoted_operations: frozenset[str | None],
        *,
        globally_demoted: bool,
    ) -> None:
        """Refresh the demotion markers for one model from the optimizer's derivation."""
        if demoted_operations:
            self._demoted_operations[name] = demoted_operations
        else:
            self._demoted_operations.pop(name, None)
        if globally_demoted:
            self._globally_demoted.add(name)
        else:
            self._globally_demoted.discard(name)

    def demoted_operations(self, name: str) -> frozenset[str | None]:
        return self._demoted_operations.get(name, frozenset())

    def is_globally_demoted(self, name: str) -> bool:
        return name in self._globally_demoted

    def tier_of(self, name: str, operation: str | None) -> int:
        """The tier a candidate falls into for ``operation`` — lower is better."""
        if operation in self._demoted_operations.get(name, frozenset()):
            return TIER_DEMOTED_EVIDENCED
        if name in self._globally_demoted:
            return TIER_DEMOTED_UNTRIED
        if name in self._deprecated:
            return TIER_DEPRECATED
        return TIER_NORMAL

    def _maybe_alert_degraded_tier(self, name: str, operation: str | None, tier: int) -> None:
        """Keyed on tier as well as (name, operation): an escalation to a worse tier
        always alerts even inside another tier's realert window."""
        if self._on_degraded_tier is None or tier <= TIER_NORMAL:
            return
        key = (name, operation, tier)
        now = time.monotonic()
        if now - self._last_degraded_alert.get(key, float("-inf")) < _DEGRADED_TIER_ALERT_INTERVAL:
            return
        self._last_degraded_alert[key] = now
        self._on_degraded_tier(name, operation, tier)

    # ------------------------------------------------------------------
    # Slot acquisition
    # ------------------------------------------------------------------

    async def _queue_acquire(self, wait: float | None) -> LLMConfig:
        if wait is None:
            return await self._queue.get()
        if wait == 0:
            return self._queue.get_nowait()
        return await asyncio.wait_for(self._queue.get(), timeout=wait)

    def _partition_by_tier(
        self,
        available: list[LLMConfig],
        operation: str | None,
    ) -> tuple[int, list[LLMConfig], list[LLMConfig]]:
        """Split into (best_tier, best_tier_candidates, everything_else)."""
        tiered: dict[int, list[LLMConfig]] = {}
        for cfg in available:
            tiered.setdefault(self.tier_of(cfg.name, operation), []).append(cfg)
        best_tier = min(tiered)
        candidates = tiered.pop(best_tier)
        others = [cfg for lst in tiered.values() for cfg in lst]
        return best_tier, candidates, others

    async def acquire(
        self,
        wait: float | None,
        *,
        policy: SelectionPolicy | None = None,
        operation: str | None = None,
    ) -> LLMConfig:
        first = await self._queue_acquire(wait)
        if policy is None:
            return first
        available = [first]
        while True:
            try:
                available.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        best_tier, candidates, others = self._partition_by_tier(available, operation)
        try:
            picked = policy.select(candidates, operation=operation)
        except Exception:
            for cfg in available:
                self._reenqueue_config(cfg)
            raise
        if picked is None:
            picked = candidates[0]
        for cfg in candidates:
            if cfg is not picked:
                self._reenqueue_config(cfg)
        for cfg in others:
            self._reenqueue_config(cfg)
        self._maybe_alert_degraded_tier(picked.name, operation, best_tier)
        return picked

    def release(self, config: LLMConfig) -> None:
        self._reenqueue_config(config)

    # ------------------------------------------------------------------
    # Cooldown / state
    # ------------------------------------------------------------------

    def clear_cooling(self, name: str) -> None:
        self._state.clear_cooling(name)

    def _reenqueue_config(self, config: LLMConfig) -> None:
        """Re-admit a slot to circulation — refuses a config that got benched meanwhile."""
        if config.name in self._benched:
            return
        self._queue.put_nowait(config)

    async def cool_down(self, config: LLMConfig, delay: float) -> None:
        """Withdraw the slot for ``delay`` seconds, persisting the new state."""
        cooldown_until = datetime.now(UTC) + timedelta(seconds=delay)
        self._state.set_cooling(
            config.name,
            cooldown_until,
            self._state.fail_count(config.name) + 1,
        )
        if self._state_store is not None:
            await self._state_store.write(
                config.name,
                self._state.get_state(config.name),
                self._user_id,
            )
            self._store_cache = None
        loop = asyncio.get_running_loop()
        loop.call_later(float(delay), self._reenqueue_config, config)
        logger.warning("LLM %s cooling for %ds", config.name, delay)

    def mark_quality_fail(self, name: str) -> None:
        self._state.record_quality_fail(name)

    def state(self, name: str) -> LLMState:
        return self._state.get_state(name)

    async def stored_states(self) -> dict[str, LLMState]:
        """Read persisted per-LLM state from the store, or ``{}`` if none configured."""
        if self._state_store is None:
            return {}
        return await self._state_store.read(self._user_id)

    async def _get_store_cache(self) -> dict[str, LLMState]:
        now = time.monotonic()
        if self._store_cache is not None and now < self._store_cache_expires:
            return self._store_cache
        self._store_cache = await self.stored_states()
        self._store_cache_expires = now + _STORE_CACHE_TTL
        return self._store_cache

    async def apply_shared_cooling(self, config: LLMConfig) -> bool:
        """Return True if shared store shows this LLM cooling and the slot was deferred.

        The caller must have already dequeued ``config`` via ``acquire()``; this method
        re-queues it via ``call_later`` and must not be called on a slot still in the queue.
        """
        if self._state_store is None:
            return False
        try:
            shared = await self._get_store_cache()
        except Exception:  # noqa: BLE001
            logger.warning(
                "LLM %s: shared-state read failed; proceeding without shared cooling",
                config.name,
            )
            return False
        stored = shared.get(config.name)
        if stored is None or stored.phase is not LifecyclePhase.COOLING:
            return False
        # A naive cooldown_until can't be compared to the aware now_utc below;
        # treat it as "not cooling" rather than risk a TypeError mid-routing.
        if stored.cooldown_until is None or stored.cooldown_until.tzinfo is None:
            return False
        now_utc = datetime.now(UTC)
        if stored.cooldown_until <= now_utc:
            return False
        local_fail_count = self._state.fail_count(config.name)
        self._state.set_cooling(
            config.name,
            stored.cooldown_until,
            max(local_fail_count, stored.fail_count),
        )
        delay = (stored.cooldown_until - now_utc).total_seconds()
        loop = asyncio.get_running_loop()
        loop.call_later(delay, self._reenqueue_config, config)
        logger.info("LLM %s: shared cooldown applied, %.0fs remaining", config.name, delay)
        return True

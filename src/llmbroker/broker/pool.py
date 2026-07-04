"""The live in-memory routing substrate behind ``AsyncBroker``.

Each LLM has one ``_Slot`` carrying its config, resolved key, and live
cooldown/quality/in-flight state. ``acquire()`` picks an available slot under
an ``asyncio.Condition`` and blocks (optionally with a deadline) until one
frees up or a cooldown expires. Parallel calls to one LLM are allowed by
default; ``LLMConfig.parallel`` caps simultaneous in-flight requests per slot
(1 = serialize).

Tiered selection: candidates for an operation partition into tier 0 (normal),
tier 1 (``deprecated`` — curator withdrew the endorsement), tier 2 (a
globally-demoted model's untried operation — new evidence territory), tier 3
(quality-demoted for this specific operation — evidenced bad). ``acquire()``
serves the best non-empty tier; the caller's ``SelectionPolicy`` ranks only
within that tier. ``deprecated``/demotion markers are soft (consulted at
acquire time, never withdraw a slot) — only ``disabled`` and a missing key
make a slot unavailable.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

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


@dataclass
class _Slot:
    """Live per-LLM state: static config plus everything routing needs to know now."""

    config: LLMConfig
    key: str | None = None
    in_flight: int = 0  # count of concurrently running calls; capped by config.parallel
    cooldown_until: datetime | None = None  # aware UTC
    fail_count: int = 0
    disabled: bool = False  # manual admin verdict
    deprecated: bool = False
    pick_seq: int = 0  # monotone counter for round-robin


class LLMPool:
    """The pool of LLM slots and their live cooldown / quality state."""

    def __init__(
        self,
        state_store: StateStoreProtocol | None,
        user_id: int | str | None,
        *,
        on_degraded_tier: Callable[[str, str | None, int], None] | None = None,
    ) -> None:
        self._slots: dict[str, _Slot] = {}
        self._cond = asyncio.Condition()
        self._pick_counter = 0
        self._state_store = state_store
        self._user_id = user_id
        self._store_cache: dict[str, LLMState] | None = None
        self._store_cache_expires: float = 0.0
        self._demoted_operations: dict[str, frozenset[str | None]] = {}
        self._globally_demoted: set[str] = set()
        self._on_degraded_tier = on_degraded_tier
        self._last_degraded_alert: dict[tuple[str, str | None, int], float] = {}

    # ------------------------------------------------------------------
    # Membership / lookup
    # ------------------------------------------------------------------

    def __contains__(self, name: str) -> bool:
        return name in self._slots

    def __len__(self) -> int:
        return len(self._slots)

    @property
    def configs(self) -> dict[str, LLMConfig]:
        return {name: slot.config for name, slot in self._slots.items()}

    def config(self, name: str) -> LLMConfig:
        return self._slots[name].config

    def has_key(self, name: str) -> bool:
        slot = self._slots.get(name)
        return slot is not None and slot.key is not None

    def resolved_key(self, name: str) -> str:
        slot = self._slots[name]
        if slot.key is None:
            raise KeyError(name)
        return slot.key

    # ------------------------------------------------------------------
    # Membership mutation
    # ------------------------------------------------------------------

    async def add(self, cfg: LLMConfig, key: str | None) -> None:
        """Register/refresh a config. A ``None`` key leaves any prior key intact.

        Upserts in place so an existing slot's live state (cooldown, fail count,
        in-flight, disabled, deprecated) survives a config refresh.
        """
        async with self._cond:
            slot = self._slots.get(cfg.name)
            if slot is None:
                self._slots[cfg.name] = _Slot(config=cfg, key=key)
            else:
                slot.config = cfg
                if key is not None:
                    slot.key = key
            self._cond.notify_all()

    async def drop(self, name: str) -> None:
        """Remove a slot entirely, so a later re-add under the same name starts clean
        rather than inheriting stale tier/alert state."""
        async with self._cond:
            self._slots.pop(name, None)
            self._demoted_operations.pop(name, None)
            self._globally_demoted.discard(name)
            for key in [k for k in self._last_degraded_alert if k[0] == name]:
                del self._last_degraded_alert[key]
            self._cond.notify_all()

    # ------------------------------------------------------------------
    # Manual disable (hard exclusion) / deprecated / demotion markers (soft, tiered)
    # ------------------------------------------------------------------

    def set_disabled(self, name: str) -> None:
        """Withdraw the slot. An in-flight call finishes normally; the flag excludes
        the slot from every acquisition afterward."""
        slot = self._slots.get(name)
        if slot is not None:
            slot.disabled = True

    async def clear_disabled(self, name: str) -> None:
        async with self._cond:
            slot = self._slots.get(name)
            if slot is not None:
                slot.disabled = False
            self._cond.notify_all()

    def is_disabled(self, name: str) -> bool:
        slot = self._slots.get(name)
        return slot is not None and slot.disabled

    def set_deprecated(self, name: str) -> None:
        slot = self._slots.get(name)
        if slot is not None:
            slot.deprecated = True

    def clear_deprecated(self, name: str) -> None:
        slot = self._slots.get(name)
        if slot is not None:
            slot.deprecated = False

    def is_deprecated(self, name: str) -> bool:
        slot = self._slots.get(name)
        return slot is not None and slot.deprecated

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
        if self.is_deprecated(name):
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

    def _available(self, slot: _Slot, now: datetime) -> bool:
        cap = slot.config.parallel
        return (
            slot.key is not None
            and not slot.disabled
            and (cap is None or slot.in_flight < cap)
            and (slot.cooldown_until is None or slot.cooldown_until <= now)
        )

    def _partition_by_tier(
        self,
        available: list[_Slot],
        operation: str | None,
    ) -> tuple[int, list[_Slot]]:
        """Split into (best_tier, best_tier_slots)."""
        tiered: dict[int, list[_Slot]] = {}
        for slot in available:
            tiered.setdefault(self.tier_of(slot.config.name, operation), []).append(slot)
        best_tier = min(tiered)
        return best_tier, tiered[best_tier]

    def _wake_timeout(self, now: datetime, deadline: float | None) -> float | None:
        """Seconds until the nearest event that could make a slot available, or
        ``None`` when nothing is scheduled (wait solely on notification)."""
        candidates: list[float] = []
        for slot in self._slots.values():
            if slot.key is None or slot.disabled or slot.in_flight:
                continue
            if slot.cooldown_until is not None and slot.cooldown_until > now:
                candidates.append((slot.cooldown_until - now).total_seconds())
        if deadline is not None:
            candidates.append(deadline - time.monotonic())
        return min(candidates) if candidates else None

    async def acquire(
        self,
        wait: float | None,
        *,
        policy: SelectionPolicy | None = None,
        operation: str | None = None,
    ) -> LLMConfig:
        deadline = None if wait is None else time.monotonic() + wait
        async with self._cond:
            while True:
                now = datetime.now(UTC)
                avail = [s for s in self._slots.values() if self._available(s, now)]
                if avail:
                    best_tier, candidates = self._partition_by_tier(avail, operation)
                    picked_cfg = (
                        policy.select([s.config for s in candidates], operation=operation)
                        if policy is not None
                        else None
                    )
                    slot = (
                        self._slots[picked_cfg.name]
                        if picked_cfg is not None
                        else min(candidates, key=lambda s: s.pick_seq)
                    )
                    slot.in_flight += 1
                    self._pick_counter += 1
                    slot.pick_seq = self._pick_counter
                    self._maybe_alert_degraded_tier(slot.config.name, operation, best_tier)
                    return slot.config
                if wait == 0:
                    raise TimeoutError("no LLM slot available and wait=0")
                timeout = self._wake_timeout(now, deadline)
                if deadline is not None and timeout is not None and timeout <= 0:
                    raise TimeoutError("no LLM slot came free within wait")
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout)
                except TimeoutError:
                    # Re-check: a cooldown may have expired, or the deadline hit (next loop raises).
                    continue

    async def release(self, config: LLMConfig) -> None:
        """A missing name is legal (removed mid-flight) — no-op."""
        async with self._cond:
            slot = self._slots.get(config.name)
            if slot is not None:
                slot.in_flight = max(0, slot.in_flight - 1)
            self._cond.notify_all()

    # ------------------------------------------------------------------
    # Cooldown / state
    # ------------------------------------------------------------------

    def clear_cooling(self, name: str) -> None:
        slot = self._slots.get(name)
        if slot is not None:
            slot.cooldown_until = None

    async def cool_down(self, config: LLMConfig, delay: float) -> None:
        """Withdraw the slot for ``delay`` seconds, persisting the new state."""
        cooldown_until = datetime.now(UTC) + timedelta(seconds=delay)
        async with self._cond:
            slot = self._slots.get(config.name)
            if slot is not None:
                slot.cooldown_until = cooldown_until
                slot.fail_count += 1
                slot.in_flight = max(0, slot.in_flight - 1)
            self._cond.notify_all()
        if slot is not None and self._state_store is not None:
            await self._state_store.write(
                config.name,
                self.state(config.name),
                self._user_id,
            )
            self._store_cache = None
        logger.warning("LLM %s cooling for %ds", config.name, delay)

    def mark_quality_fail(self, name: str) -> None:
        slot = self._slots.get(name)
        if slot is not None:
            slot.fail_count += 1

    def state(self, name: str) -> LLMState:
        slot = self._slots.get(name)
        if slot is None:
            return LLMState()
        now = datetime.now(UTC)
        if slot.cooldown_until is not None and slot.cooldown_until > now:
            return LLMState(
                phase=LifecyclePhase.COOLING,
                cooldown_until=slot.cooldown_until,
                fail_count=slot.fail_count,
            )
        return LLMState(
            phase=LifecyclePhase.AVAILABLE,
            cooldown_until=None,
            fail_count=slot.fail_count,
        )

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

    @staticmethod
    def _active_shared_cooldown(stored: LLMState | None, now: datetime) -> datetime | None:
        """The stored cooldown_until, or ``None`` if it does not represent an active cooldown.

        A naive cooldown_until can't be compared to an aware ``now``; treat it as "not
        cooling" rather than risk a TypeError mid-routing.
        """
        if stored is None or stored.phase is not LifecyclePhase.COOLING:
            return None
        if stored.cooldown_until is None or stored.cooldown_until.tzinfo is None:
            return None
        if stored.cooldown_until <= now:
            return None
        return stored.cooldown_until

    async def apply_shared_cooling(self, config: LLMConfig) -> bool:
        """Return True if shared store shows this LLM cooling and the slot was deferred.

        The caller must have already acquired ``config`` via ``acquire()`` (so its
        slot is marked in-flight); this clears that claim and marks it cooling instead.
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
        now_utc = datetime.now(UTC)
        stored = shared.get(config.name)
        cooldown_until = self._active_shared_cooldown(stored, now_utc)
        slot = self._slots.get(config.name)
        if cooldown_until is None or slot is None or stored is None:
            return False
        slot.fail_count = max(slot.fail_count, stored.fail_count)
        slot.cooldown_until = cooldown_until
        slot.in_flight = max(0, slot.in_flight - 1)
        delay = (cooldown_until - now_utc).total_seconds()
        logger.info("LLM %s: shared cooldown applied, %.0fs remaining", config.name, delay)
        return True

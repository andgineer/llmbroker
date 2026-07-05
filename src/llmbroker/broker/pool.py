"""The live in-memory routing substrate behind ``AsyncBroker``.

Each LLM has one ``_Slot`` carrying its config, resolved key, and live
cooldown/quality/in-flight state. ``acquire()`` picks an available slot under
an ``asyncio.Condition`` and blocks (optionally with a deadline) until one
frees up or a cooldown expires. Parallel calls to one LLM are allowed by
default; ``LLMConfig.parallel`` caps simultaneous in-flight requests per slot
(1 = serialize).

Selection is one sort key: a slot quality-demoted for the requested operation
(see ``Optimizer.is_demoted``) sorts after every non-demoted slot; among slots
with the same demotion verdict, curated priority wins (``_Slot.order``, the
model's position in the registry/preset — lower is better). Demotion is soft
(consulted at acquire time, never withdraws a slot) — only ``disabled`` and a
missing key make a slot unavailable.

Pool state is not itself persisted: cooldowns are local until a peer's failure
surfaces through the journal rebuild, which calls ``apply_peer_cooldowns``.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from llmbroker.models import LifecyclePhase, LLMConfig, LLMState
from llmbroker.optimizer import Optimizer

logger = logging.getLogger("llmbroker.broker")


@dataclass
class _Slot:
    """Live per-LLM state: static config plus everything routing needs to know now."""

    config: LLMConfig
    key: str | None = None
    in_flight: int = 0  # count of concurrently running calls; capped by config.parallel
    cooldown_until: datetime | None = None  # aware UTC
    fail_count: int = 0
    disabled: bool = False  # manual admin verdict
    order: int = 0  # curated priority: registry/preset position, lower is better


class LLMPool:
    """The pool of LLM slots and their live cooldown / quality state."""

    def __init__(self, *, optimizer: Optimizer | None = None) -> None:
        self._slots: dict[str, _Slot] = {}
        self._cond = asyncio.Condition()
        self._next_order = 0
        self._optimizer = optimizer

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

    async def add(self, cfg: LLMConfig, key: str | None, order: int | None = None) -> None:
        """Register/refresh a config. A ``None`` key leaves any prior key intact.

        Upserts in place so an existing slot's live state (cooldown, fail count,
        in-flight, disabled) survives a config refresh. ``order`` defaults to
        insertion order when the caller has no curated position to assert.
        """
        async with self._cond:
            resolved_order = order if order is not None else self._next_order
            self._next_order = max(self._next_order, resolved_order + 1)
            slot = self._slots.get(cfg.name)
            if slot is None:
                self._slots[cfg.name] = _Slot(config=cfg, key=key, order=resolved_order)
            else:
                slot.config = cfg
                slot.order = resolved_order
                if key is not None:
                    slot.key = key
            self._cond.notify_all()

    async def drop(self, name: str) -> None:
        """Remove a slot entirely, so a later re-add under the same name starts clean."""
        async with self._cond:
            self._slots.pop(name, None)
            self._cond.notify_all()

    # ------------------------------------------------------------------
    # Manual disable (hard exclusion)
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

    def _is_demoted(self, name: str, operation: str | None) -> bool:
        return self._optimizer is not None and self._optimizer.is_demoted(name, operation)

    def demoted_operations(self, name: str) -> frozenset[str | None]:
        return (
            self._optimizer.demoted_operations(name) if self._optimizer is not None else frozenset()
        )

    def _wake_timeout(self, now: datetime, deadline: float | None) -> float | None:
        """Seconds until the nearest event that could make a slot available, or
        ``None`` when nothing is scheduled (wait solely on notification)."""
        candidates: list[float] = []
        for slot in self._slots.values():
            cap = slot.config.parallel
            if slot.key is None or slot.disabled or (cap is not None and slot.in_flight >= cap):
                continue
            if slot.cooldown_until is not None and slot.cooldown_until > now:
                candidates.append((slot.cooldown_until - now).total_seconds())
        if deadline is not None:
            candidates.append(deadline - time.monotonic())
        return min(candidates) if candidates else None

    async def acquire(self, wait: float | None, *, operation: str | None = None) -> LLMConfig:
        deadline = None if wait is None else time.monotonic() + wait
        async with self._cond:
            while True:
                now = datetime.now(UTC)
                avail = [s for s in self._slots.values() if self._available(s, now)]
                if avail:
                    slot = min(
                        avail,
                        key=lambda s: (self._is_demoted(s.config.name, operation), s.order),
                    )
                    slot.in_flight += 1
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
        """Withdraw the slot for ``delay`` seconds."""
        cooldown_until = datetime.now(UTC) + timedelta(seconds=delay)
        async with self._cond:
            slot = self._slots.get(config.name)
            if slot is not None:
                slot.cooldown_until = cooldown_until
                slot.fail_count += 1
                slot.in_flight = max(0, slot.in_flight - 1)
            self._cond.notify_all()
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

    async def apply_peer_cooldowns(
        self,
        cooldowns: dict[str, datetime],
        fail_counts: dict[str, int] | None = None,
    ) -> None:
        """Raise each named slot's ``cooldown_until`` to at least the given value.

        Called from the debounced journal rebuild — never lowers an already-later
        local cooldown, and never touches ``in_flight`` (nothing was acquired in
        this code path). The peer fail-streak folds in as ``max(local, peer)``.
        """
        fail_counts = fail_counts or {}
        async with self._cond:
            changed = False
            for name, until in cooldowns.items():
                slot = self._slots.get(name)
                if slot is None:
                    continue
                if slot.cooldown_until is None or until > slot.cooldown_until:
                    slot.cooldown_until = until
                    changed = True
            for name, count in fail_counts.items():
                slot = self._slots.get(name)
                if slot is not None and count > slot.fail_count:
                    slot.fail_count = count
            if changed:
                self._cond.notify_all()

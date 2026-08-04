"""LLMPool: live per-LLM slot state (config, key, cooldown, quality) backing routing."""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from llmbroker.exceptions import NoLLMAvailableError
from llmbroker.models import LifecyclePhase, LLMConfig, LLMState
from llmbroker.optimizer import Optimizer

logger = logging.getLogger("llmbroker.broker")

# A recorded bound and a caller's remaining budget both carry sub-millisecond noise,
# so "the same wait twice" would otherwise be a coin flip. One second is a physical
# statement, not a ratio: at LLM latencies, smaller budget differences are noise.
_BUDGET_SLACK_SEC = 1.0

# How long one observed miss keeps ordering weight. The router's own miss and the
# rebuild's derivation from the tail both age by it, so evidence cannot outlive
# itself in one path and not the other.
BUDGET_BOUND_WINDOW_SEC = 600.0


@dataclass(frozen=True, slots=True)
class _Bound:
    """The largest budget a model recently missed, and when that evidence lapses."""

    seconds: float
    until: datetime  # aware UTC


@dataclass
class _Slot:
    """Live per-LLM state: static config plus everything routing needs to know now."""

    config: LLMConfig
    key: str | None = None
    in_flight: int = 0  # count of concurrently running calls; capped by config.parallel
    cooldown_until: datetime | None = None  # aware UTC
    fail_count: int = 0
    disabled: bool = False  # manual admin verdict
    order: int = 0  # tiebreaker only: registry/preset position, lower is better


class LLMPool:
    """The pool of LLM slots and their live cooldown / quality state."""

    def __init__(self, *, optimizer: Optimizer | None = None) -> None:
        self._slots: dict[str, _Slot] = {}
        self._cond = asyncio.Condition()
        self._next_order = 0
        self._optimizer = optimizer
        self._budget_bounds: dict[str, _Bound] = {}

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
        """Register/refresh a config and the key it currently resolves to.

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
                # A key that no longer resolves withdraws the slot immediately.
                # Keeping the old value would route real requests at a revoked
                # key until the journal condemned it.
                slot.key = key
            self._cond.notify_all()

    async def drop(self, name: str) -> None:
        """Remove a slot entirely, so a later re-add under the same name starts clean."""
        async with self._cond:
            self._slots.pop(name, None)
            self._budget_bounds.pop(name, None)
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

    def _is_demoted(self, name: str, operation: str | None) -> bool:
        return self._optimizer is not None and self._optimizer.is_demoted(name, operation)

    def _priority(self, slot: _Slot, operation: str | None) -> float:
        """The curated weight, shrunk toward host ratings as they accumulate. Falls
        back to the raw weight with no optimizer."""
        if self._optimizer is None:
            return slot.config.weight
        return self._optimizer.quality_score(slot.config.name, operation, slot.config.weight)

    async def apply_budget_bounds(self, observed: dict[str, tuple[float, datetime]]) -> None:
        """Replace the map of recently-missed answer budgets, each given as the
        budget in seconds and the instant it was observed.

        Wholesale, like the quality windows: a rebuild is a fresh derivation over
        the journal tail, not an increment on what is already here.
        """
        async with self._cond:
            self._budget_bounds = {
                name: _Bound(budget, at + timedelta(seconds=BUDGET_BOUND_WINDOW_SEC))
                for name, (budget, at) in observed.items()
            }

    def raise_budget_bound(self, name: str, budget: float, observed_at: datetime) -> None:
        """Record one just-observed miss, so the next caller offering no more than
        ``budget`` seconds is handed a sibling first."""
        current = self._budget_bounds.get(name)
        # A lapsed window is spent evidence, not a floor to build on: carrying the
        # old number over would let one small miss re-arm a far larger bound the
        # window had already retired, for budgets nobody is offering any more.
        previous = current.seconds if current is not None and current.until > observed_at else 0.0
        self._budget_bounds[name] = _Bound(
            max(previous, budget),
            observed_at + timedelta(seconds=BUDGET_BOUND_WINDOW_SEC),
        )

    def clear_budget_bound(self, name: str) -> None:
        self._budget_bounds.pop(name, None)

    def _over_budget(self, slot: _Slot, remaining: float | None, now: datetime) -> bool:
        """Whether this LLM has recently failed to answer within a budget as small as
        the one on offer — a reason to prefer a sibling, never to exclude it."""
        bound = self._budget_bounds.get(slot.config.name)
        if remaining is None or bound is None or bound.until <= now:
            return False
        return remaining < bound.seconds + _BUDGET_SLACK_SEC

    def demoted_operations(self, name: str) -> frozenset[str | None]:
        return (
            self._optimizer.demoted_operations(name) if self._optimizer is not None else frozenset()
        )

    def _wake_timeout(
        self,
        now: datetime,
        queue_deadline: float | None,
        candidates: list[_Slot],
    ) -> float | None:
        """Seconds until the nearest event that could make a candidate available, or
        ``None`` when nothing is scheduled (wait solely on notification)."""
        wakeups: list[float] = []
        for slot in candidates:
            cap = slot.config.parallel
            if cap is not None and slot.in_flight >= cap:
                continue
            if slot.cooldown_until is not None and slot.cooldown_until > now:
                wakeups.append((slot.cooldown_until - now).total_seconds())
        if queue_deadline is not None:
            wakeups.append(queue_deadline - time.monotonic())
        return min(wakeups) if wakeups else None

    def _exhaustion_reason(self, exclude: frozenset[str]) -> str:
        if exclude & self._slots.keys():
            return "excluded"
        if not self._slots:
            return "empty_pool"
        if not any(slot.key is not None for slot in self._slots.values()):
            return "no_keys"
        return "all_disabled"

    def _raise_exhausted(self, exclude: frozenset[str]) -> None:
        reason = self._exhaustion_reason(exclude)
        message = {
            "excluded": "every candidate model was excluded for this request",
            "empty_pool": "the LLM pool has no slots",
            "no_keys": (
                "no LLM has a resolved api_key_ref — set at least one env var or configure"
                " a secrets backend"
            ),
            "all_disabled": "every LLM is administratively disabled",
        }[reason]
        raise NoLLMAvailableError(message, reason=reason)

    async def acquire(
        self,
        queue_deadline: float | None,
        *,
        operation: str | None = None,
        exclude: frozenset[str] = frozenset(),
        answer_deadline: float | None = None,
    ) -> LLMConfig:
        async with self._cond:
            while True:
                now = datetime.now(UTC)
                # Recomputed per iteration: the longer the queue wait, the less budget is
                # left for the answer, and the stricter the choice below becomes.
                remaining = None if answer_deadline is None else answer_deadline - time.monotonic()
                candidates = [
                    s
                    for s in self._slots.values()
                    if s.key is not None and not s.disabled and s.config.name not in exclude
                ]
                avail = [
                    s
                    for s in candidates
                    if (s.config.parallel is None or s.in_flight < s.config.parallel)
                    and (s.cooldown_until is None or s.cooldown_until <= now)
                ]
                if avail:
                    slot = min(
                        avail,
                        key=lambda s: (
                            self._over_budget(s, remaining, now),
                            self._is_demoted(s.config.name, operation),
                            -self._priority(s, operation),
                            s.order,
                        ),
                    )
                    slot.in_flight += 1
                    return slot.config
                if not candidates:
                    self._raise_exhausted(exclude)
                if queue_deadline is not None and time.monotonic() >= queue_deadline:
                    cooling = [s.cooldown_until for s in candidates if s.cooldown_until is not None]
                    retry_at = min(cooling) if cooling else None
                    raise NoLLMAvailableError(
                        "no LLM slot came free within wait",
                        reason="timeout",
                        retry_at=retry_at,
                    )
                timeout = self._wake_timeout(now, queue_deadline, candidates)
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

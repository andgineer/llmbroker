"""LLMPool: live per-LLM slot state (config, cooldown, quality) backing routing.

The pool holds no key: which refs a caller can pay for arrives per acquisition."""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from llmbroker.exceptions import NoLLMAvailableError
from llmbroker.models import LifecyclePhase, LLMConfig, LLMState
from llmbroker.optimizer import Optimizer

logger = logging.getLogger("llmbroker.broker")

# Both sides carry sub-millisecond noise, so "the same wait twice" would otherwise
# be a coin flip. At LLM latencies a smaller difference is noise.
_BUDGET_SLACK_SEC = 1.0

# How long one observed miss keeps ordering weight; both the router's own miss and
# the rebuild's derivation age by it, so neither outlives the other.
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
    in_flight: int = 0  # count of concurrently running calls; capped by config.parallel
    cooldown_until: datetime | None = None  # aware UTC
    fail_count: int = 0
    disabled: bool = False  # manual admin verdict
    order: int = 0  # tiebreaker only: registry/preset position, lower is better
    # Whether the next attempt on this entry is the pool rechecking its own negative
    # availability state, and whether one caller is already making it.
    recovery_due: bool = False
    recovery_claimed: bool = False


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

    # ------------------------------------------------------------------
    # Membership mutation
    # ------------------------------------------------------------------

    async def add(self, cfg: LLMConfig, order: int | None = None) -> None:
        """Register or refresh a config. Upserts in place, so a slot's live state
        survives; ``order`` defaults to insertion order where the caller asserts no
        curated position."""
        async with self._cond:
            resolved_order = order if order is not None else self._next_order
            self._next_order = max(self._next_order, resolved_order + 1)
            slot = self._slots.get(cfg.name)
            if slot is None:
                self._slots[cfg.name] = _Slot(config=cfg, order=resolved_order)
            else:
                slot.config = cfg
                slot.order = resolved_order
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
        """Replace the map of recently-missed answer budgets — seconds and the instant
        observed. Wholesale like the quality windows: a rebuild derives afresh."""
        async with self._cond:
            self._budget_bounds = {
                name: _Bound(budget, at + timedelta(seconds=BUDGET_BOUND_WINDOW_SEC))
                for name, (budget, at) in observed.items()
            }

    def raise_budget_bound(self, name: str, budget: float, observed_at: datetime) -> None:
        """Record one just-observed miss, so the next caller offering no more than
        ``budget`` seconds is handed a sibling first."""
        current = self._budget_bounds.get(name)
        # A lapsed window is spent evidence: carried over, one small miss would
        # re-arm a far larger bound the window had already retired.
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

    def _exhaustion_reason(self, exclude: frozenset[str], payable: frozenset[str]) -> str:
        if exclude & self._slots.keys():
            return "excluded"
        if not self._slots:
            return "empty_pool"
        if not any(slot.config.api_key_ref in payable for slot in self._slots.values()):
            return "no_keys"
        return "all_disabled"

    def _raise_exhausted(self, exclude: frozenset[str], payable: frozenset[str]) -> None:
        reason = self._exhaustion_reason(exclude, payable)
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

    def _candidates(self, payable: frozenset[str], exclude: frozenset[str]) -> list[_Slot]:
        return [
            s
            for s in self._slots.values()
            if s.config.api_key_ref in payable and not s.disabled and s.config.name not in exclude
        ]

    @staticmethod
    def _is_free(slot: _Slot, now: datetime) -> bool:
        """A recovery already being made is not free whatever the slot's capacity: a
        second caller would make the same unprotected call the claim exists to cover."""
        return (
            not slot.recovery_claimed
            and (slot.config.parallel is None or slot.in_flight < slot.config.parallel)
            and (slot.cooldown_until is None or slot.cooldown_until <= now)
        )

    @staticmethod
    def _earliest_return(candidates: list[_Slot], now: datetime) -> datetime | None:
        cooling = [
            s.cooldown_until
            for s in candidates
            if s.cooldown_until is not None and s.cooldown_until > now
        ]
        return min(cooling) if cooling else None

    def retry_at(
        self,
        payable: frozenset[str],
        *,
        exclude: frozenset[str] = frozenset(),
    ) -> datetime | None:
        """When a candidate comes back on its own, or ``None`` where one can serve
        right now — the same answer a queue that timed out is given, for a caller whose
        own clock ran out instead."""
        now = datetime.now(UTC)
        candidates = self._candidates(payable, exclude)
        if any(self._is_free(s, now) for s in candidates):
            return None
        return self._earliest_return(candidates, now)

    def _rank(
        self,
        slot: _Slot,
        remaining: float | None,
        now: datetime,
        operation: str | None,
    ) -> tuple[bool, bool, float, int]:
        """The one ordering key every acquisition sorts on, so a parallel call cannot
        pick differently from a sequential one."""
        return (
            self._over_budget(slot, remaining, now),
            self._is_demoted(slot.config.name, operation),
            -self._priority(slot, operation),
            slot.order,
        )

    def _reserve(  # noqa: PLR0913 - one selection: from what, how many, and against what
        self,
        candidates: list[_Slot],
        *,
        width: int,
        recovery_width: int,
        remaining: float | None,
        now: datetime,
        operation: str | None,
    ) -> list[LLMConfig]:
        """Take the best free candidates, marked in flight before the condition is
        released so no two callers reserve one slot. Where the best of them is a
        recovery attempt, ``recovery_width`` applies and an ordinary entry covers it."""
        ranked = sorted(
            (s for s in candidates if self._is_free(s, now)),
            key=lambda s: self._rank(s, remaining, now, operation),
        )
        best = ranked[:width]
        if best and best[0].recovery_due:
            best += self._cover(ranked[width:], recovery_width - width)
        for slot in best:
            slot.in_flight += 1
            slot.recovery_claimed = slot.recovery_due
        return [slot.config for slot in best]

    @staticmethod
    def _cover(rest: list[_Slot], count: int) -> list[_Slot]:
        """What runs beside a recovery attempt: an entry the pool has no open question
        about, so one recheck is not covered by another where an ordinary one is free."""
        if count <= 0:
            return []
        ordinary = [s for s in rest if not s.recovery_due]
        return (ordinary + [s for s in rest if s.recovery_due])[:count]

    async def acquire(  # noqa: PLR0913 - one attempt's whole context, all optional
        self,
        queue_deadline: float | None,
        *,
        payable: frozenset[str],
        operation: str | None = None,
        exclude: frozenset[str] = frozenset(),
        answer_deadline: float | None = None,
    ) -> LLMConfig:
        """Take a slot for one attempt. ``payable`` names the refs the calling caller
        holds a key for — a model it cannot pay for is not a candidate."""
        taken = await self.acquire_many(
            queue_deadline,
            payable=payable,
            operation=operation,
            exclude=exclude,
            answer_deadline=answer_deadline,
        )
        return taken[0]

    async def acquire_many(  # noqa: PLR0913 - one call's whole context: who, how many, how long
        self,
        queue_deadline: float | None,
        *,
        payable: frozenset[str],
        width: int = 1,
        recovery_width: int = 1,
        operation: str | None = None,
        exclude: frozenset[str] = frozenset(),
        answer_deadline: float | None = None,
    ) -> list[LLMConfig]:
        """Take up to ``width`` distinct slots for one call, waiting as ``wait`` allows
        for the first of them and taking whatever else is free by then — never fewer
        than one, and never waiting for a second."""
        async with self._cond:
            while True:
                now = datetime.now(UTC)
                # Recomputed per iteration: the longer the queue wait, the less budget is
                # left for the answer, and the stricter the choice below becomes.
                remaining = None if answer_deadline is None else answer_deadline - time.monotonic()
                candidates = self._candidates(payable, exclude)
                taken = self._reserve(
                    candidates,
                    width=width,
                    recovery_width=recovery_width,
                    remaining=remaining,
                    now=now,
                    operation=operation,
                )
                if taken:
                    return taken
                if not candidates:
                    self._raise_exhausted(exclude, payable)
                if queue_deadline is not None and time.monotonic() >= queue_deadline:
                    raise NoLLMAvailableError(
                        "no LLM slot came free within wait",
                        reason="timeout",
                        retry_at=self._earliest_return(candidates, now),
                    )
                timeout = self._wake_timeout(now, queue_deadline, candidates)
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout)
                except TimeoutError:
                    # Re-check: a cooldown may have expired, or the deadline hit (next loop raises).
                    continue

    async def take_free(
        self,
        *,
        payable: frozenset[str],
        width: int,
        operation: str | None = None,
        exclude: frozenset[str] = frozenset(),
        answer_deadline: float | None = None,
    ) -> list[LLMConfig]:
        """Whatever is free this instant, up to ``width``, or nothing: it never waits
        and never raises, because the lanes it tops up are already racing."""
        async with self._cond:
            now = datetime.now(UTC)
            remaining = None if answer_deadline is None else answer_deadline - time.monotonic()
            return self._reserve(
                self._candidates(payable, exclude),
                width=width,
                recovery_width=1,
                remaining=remaining,
                now=now,
                operation=operation,
            )

    async def release(self, config: LLMConfig) -> None:
        """Hand the slot back. A missing name is legal (removed mid-flight) — no-op.
        An unsettled recovery stays due: neither a rejected request nor a lane a
        sibling answered past proves the entry is back."""
        async with self._cond:
            slot = self._slots.get(config.name)
            if slot is not None:
                slot.in_flight = max(0, slot.in_flight - 1)
                slot.recovery_claimed = False
            self._cond.notify_all()

    # ------------------------------------------------------------------
    # Cooldown / state
    # ------------------------------------------------------------------

    def clear_cooling(self, name: str) -> None:
        """The entry answered: it is available again and owes no recovery attempt."""
        slot = self._slots.get(name)
        if slot is not None:
            slot.cooldown_until = None
            slot.recovery_due = False
            slot.recovery_claimed = False

    async def cool_down(self, config: LLMConfig, delay: float) -> None:
        """Withdraw the slot for ``delay`` seconds."""
        cooldown_until = datetime.now(UTC) + timedelta(seconds=delay)
        async with self._cond:
            slot = self._slots.get(config.name)
            if slot is not None:
                slot.cooldown_until = cooldown_until
                slot.fail_count += 1
                slot.in_flight = max(0, slot.in_flight - 1)
                # A new cooldown is new negative state, so the first attempt past it
                # is a recovery again whatever the last one settled.
                slot.recovery_due = True
                slot.recovery_claimed = False
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

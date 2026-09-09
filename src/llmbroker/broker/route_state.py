"""Mutable state shared by atomic and streaming pool routes."""

import asyncio
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from typing import Any

from llmbroker.broker.keyring import KeyRing
from llmbroker.broker.result import CallReceipt
from llmbroker.broker.verdict import BudgetExpired, Failed
from llmbroker.exceptions import ProviderError
from llmbroker.models import LLMConfig


@dataclass(frozen=True, slots=True)
class Attempt:
    """Identity and payer of one in-flight provider attempt."""

    config: LLMConfig
    call_id: str
    t0: float
    operation: str | None
    trace_id: str | None
    ring: KeyRing
    resolved_key: str


@dataclass(slots=True)
class Outcome:
    """Settlement facts reported by one provider attempt."""

    receipt: CallReceipt = field(default_factory=CallReceipt)
    answered: bool = False
    verdict: Failed | BudgetExpired | None = None
    superseded: bool = False
    stopped: bool = False
    completed_at: float | None = None
    completed: Callable[[], None] | None = None
    settling: bool = False
    holds_slot: bool = False
    crashed: Exception | None = None


@dataclass(slots=True)
class Lane:
    """One atomic candidate held at its first produced result."""

    config: LLMConfig
    outcome: Outcome
    produced: AsyncGenerator[Any, None]
    task: asyncio.Task[bool] | None = None
    first: Any = None

    async def open(self) -> bool:
        try:
            self.first = await anext(self.produced)
        except StopAsyncIteration:
            return False
        return True

    def done(self) -> bool:
        return self.task is not None and self.task.done()

    def opened(self) -> bool:
        return self.task is not None and self.task.result()


@dataclass(slots=True)
class RouteCall:
    """Candidate history, clocks, and settlement tasks for one routed call."""

    attempt: Callable[..., AsyncGenerator[Any, None]]
    ring: KeyRing
    operation: str | None
    trace_id: str | None
    answer_deadline: float | None
    width: int
    recovery_width: int
    client_failed: set[str] = field(default_factory=set)
    attempted: set[str] = field(default_factory=set)
    last_client_error: ProviderError | None = None
    expired: bool = False
    losers: list[asyncio.Task[None]] = field(default_factory=list)

    def absorb(self, lane: Any) -> None:
        """Fold one settled attempt into call-wide selection state."""
        verdict = lane.outcome.verdict
        if isinstance(verdict, BudgetExpired):
            self.expired = True
        elif isinstance(verdict, Failed):
            self.client_failed.add(lane.config.name)
            if verdict.error is not None:
                self.last_client_error = verdict.error

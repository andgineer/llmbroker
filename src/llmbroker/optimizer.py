"""The ``Optimizer`` knob — adaptive delay tuning and OFFLINE/PROBING FSM driver."""

import collections
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from llmbroker.models import Alert, Call, CallStatus, LifecyclePhase, LLMMetrics
from llmbroker.protocols.telemetry import QueryableTelemetryProtocol, TelemetryProtocol

if TYPE_CHECKING:
    from llmbroker.broker.pool import LLMPool


@dataclass
class Optimizer:
    """Adaptive delay store and FSM-event handler for the LLM pool."""

    judge_fraction: float = 0.0
    initial_delay: float = 60.0
    max_delay: float = 3600.0
    backoff_factor: float = 2.0
    decrease_factor: float = 0.75
    max_fail_count: int = 3
    offline_sleep: float = 300.0

    _current_delay: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _pending_alerts: list[Alert] = field(default_factory=list, init=False, repr=False)
    _rl_fail_count: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _rolling: dict[tuple[str, str | None], collections.deque] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def delay_for(self, llm_name: str) -> float:
        return self._current_delay.get(llm_name, self.initial_delay)

    def rl_fail_count(self, llm_name: str) -> int:
        return self._rl_fail_count.get(llm_name, 0)

    def on_rate_limited(self, llm_name: str) -> float:
        """Increase delay; return new value (capped at max_delay)."""
        self._rl_fail_count[llm_name] = self._rl_fail_count.get(llm_name, 0) + 1
        d = min(self.delay_for(llm_name) * self.backoff_factor, self.max_delay)
        self._current_delay[llm_name] = d
        return d

    def on_success(self, llm_name: str) -> None:
        d = max(self.delay_for(llm_name) * self.decrease_factor, self.initial_delay)
        self._current_delay[llm_name] = d
        self._rl_fail_count[llm_name] = 0

    def on_probing_start(self, llm_name: str) -> None:
        """Prime rl_fail_count so one probe failure immediately re-triggers OFFLINE."""
        self._rl_fail_count[llm_name] = self.max_fail_count - 1

    def on_probing_success(self, llm_name: str) -> None:
        self._current_delay[llm_name] = self.initial_delay
        self._rl_fail_count[llm_name] = 0

    def seed_from_metrics(self, metrics: dict[str, LLMMetrics]) -> None:
        """Warm-start: prime delay hints from persisted metrics on restart."""
        for name, m in metrics.items():
            if m.last_status in (CallStatus.RATE_LIMITED, CallStatus.UNAVAILABLE):
                self._current_delay[name] = self.max_delay

    def alerts(self) -> list[Alert]:
        result = list(self._pending_alerts)
        self._pending_alerts.clear()
        return result

    def add_alert(self, msg: str) -> None:
        self._pending_alerts.append(Alert(message=msg))

    def touch_rolling(self, llm_name: str, operation: str | None) -> None:
        self._rolling.setdefault((llm_name, operation), collections.deque())


class OptimizerTelemetry:
    """Wraps any TelemetryProtocol and drives Optimizer FSM from the live event stream."""

    def __init__(
        self,
        optimizer: Optimizer,
        inner: TelemetryProtocol,
        pool: "LLMPool",
        *,
        on_go_offline: Callable[[str], None],
    ) -> None:
        self._opt = optimizer
        self._inner = inner
        self._pool = pool
        self._on_go_offline = on_go_offline

    async def record(self, call: Call) -> None:
        try:
            await self._inner.record(call)
        finally:
            self._drive_fsm(call)

    async def record_quality(self, call_id: str, score: float) -> None:
        await self._inner.record_quality(call_id, score)

    async def metrics(
        self,
        *,
        since: datetime | None = None,
        user_id: int | str | None = None,
    ) -> dict[str, LLMMetrics]:
        if isinstance(self._inner, QueryableTelemetryProtocol):
            return await self._inner.metrics(since=since, user_id=user_id)
        return {}

    async def calls(
        self,
        *,
        limit: int,
        user_id: int | str | None = None,
    ) -> list[Call]:
        if isinstance(self._inner, QueryableTelemetryProtocol):
            return await self._inner.calls(limit=limit, user_id=user_id)
        return []

    async def purge_calls(self, *, before: datetime) -> int:
        if isinstance(self._inner, QueryableTelemetryProtocol):
            return await self._inner.purge_calls(before=before)
        return 0

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def _drive_fsm(self, call: Call) -> None:
        name = call.llm_name
        self._opt.touch_rolling(name, call.operation)

        if call.status in (CallStatus.RATE_LIMITED, CallStatus.UNAVAILABLE):
            self._opt.on_rate_limited(name)
            if self._opt.rl_fail_count(name) >= self._opt.max_fail_count:
                self._pool.set_offline(name)
                self._opt.add_alert(
                    f"{name} went OFFLINE after {self._opt.rl_fail_count(name)} failures",
                )
                self._on_go_offline(name)
        elif call.status == CallStatus.OK:
            phase = self._pool.state(name).phase
            if phase == LifecyclePhase.PROBING:
                self._opt.on_probing_success(name)
                self._pool.set_available(name)
            else:
                self._opt.on_success(name)
        elif call.status == CallStatus.ERROR:
            if self._pool.state(name).phase is LifecyclePhase.PROBING:
                self._pool.set_offline(name)
                self._on_go_offline(name)

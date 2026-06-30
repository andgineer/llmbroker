"""The ``Optimizer`` knob — adaptive delay tuning and OFFLINE/PROBING FSM driver."""

import collections
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from llmbroker.models import Alert, Call, CallStatus, LifecyclePhase, LLMConfig, LLMMetrics
from llmbroker.protocols.telemetry import QueryableTelemetryProtocol, TelemetryProtocol

if TYPE_CHECKING:
    from llmbroker.broker.pool import LLMPool


class SelectionPolicy(Protocol):
    def select(self, candidates: list[LLMConfig], *, operation: str | None) -> LLMConfig | None:
        """Pick the best candidate from the currently available list.
        None means no preference — caller may pick any."""


class FirstAvailablePolicy:
    def select(self, candidates: list[LLMConfig], *, operation: str | None) -> LLMConfig | None:  # noqa: ARG002
        return candidates[0] if candidates else None


@dataclass
class Optimizer:
    """Adaptive delay store and FSM-event handler for the LLM pool."""

    initial_delay: float = 60.0
    max_delay: float = 3600.0
    backoff_factor: float = 2.0
    decrease_factor: float = 0.75
    max_fail_count: int = 3
    max_probe_cycles: int = 5
    offline_sleep: float = 300.0
    min_sample_count: int = 10
    usable_rate_floor: float = 0.5
    exploration_fraction: float = 0.1
    rolling_window: int = 50
    background_operations: frozenset[str] = field(default_factory=frozenset)

    _current_delay: dict[str, float] = field(default_factory=dict, init=False, repr=False)
    _pending_alerts: list[Alert] = field(default_factory=list, init=False, repr=False)
    _rl_fail_count: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _probe_cycles: dict[str, int] = field(default_factory=dict, init=False, repr=False)
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
        self.reset_probe_cycles(llm_name)

    def increment_probe_cycles(self, llm_name: str) -> int:
        count = self._probe_cycles.get(llm_name, 0) + 1
        self._probe_cycles[llm_name] = count
        return count

    def probe_cycles(self, llm_name: str) -> int:
        return self._probe_cycles.get(llm_name, 0)

    def reset_probe_cycles(self, llm_name: str) -> None:
        self._probe_cycles.pop(llm_name, None)

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

    def _record_rolling(self, llm_name: str, operation: str | None, call: Call) -> None:
        key: tuple[str, str | None] = (llm_name, operation)
        if key not in self._rolling:
            self._rolling[key] = collections.deque(maxlen=self.rolling_window)
        self._rolling[key].append(call)

    def _samples(self, llm_name: str, operation: str | None) -> collections.deque:
        return self._rolling.get((llm_name, operation), collections.deque())

    def usable_rate(self, llm_name: str, operation: str | None) -> float | None:
        """Bayesian (Laplace-smoothed) rate. Returns None if fewer than min_sample_count."""
        s = self._samples(llm_name, operation)
        if len(s) < self.min_sample_count:
            return None
        ok = sum(1 for c in s if c.status == CallStatus.OK)
        return (ok + 1) / (len(s) + 2)

    def mean_latency_ms(self, llm_name: str, operation: str | None) -> float | None:
        """Mean latency of OK calls; None if no OK call recorded."""
        vals = [
            c.latency_ms
            for c in self._samples(llm_name, operation)
            if c.status == CallStatus.OK and c.latency_ms is not None
        ]
        return sum(vals) / len(vals) if vals else None


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
        self._opt._record_rolling(name, call.operation, call)  # noqa: SLF001

        if call.status in (CallStatus.RATE_LIMITED, CallStatus.UNAVAILABLE):
            self._opt.on_rate_limited(name)
            if self._opt.rl_fail_count(name) >= self._opt.max_fail_count:
                self._pool.set_offline(name)
                self._on_go_offline(name)
        elif call.status == CallStatus.OK:
            phase = self._pool.state(name).phase
            if phase == LifecyclePhase.PROBING:
                self._opt.on_probing_success(name)
                self._pool.set_available(name)
            else:
                self._opt.on_success(name)
        elif call.status == CallStatus.ERROR:
            if call.http_status in (401, 403):
                cfg = self._pool.configs.get(name)
                ref = cfg.api_key_ref if cfg else "unknown"
                self._pool.drop(name)
                self._opt.add_alert(
                    f"{name}: API key appears dead (HTTP {call.http_status})"
                    f" — check api_key_ref '{ref}'",
                )
            elif self._pool.state(name).phase is LifecyclePhase.PROBING:
                cycles = self._opt.increment_probe_cycles(name)
                if cycles >= self._opt.max_probe_cycles:
                    self._pool.drop(name)
                    self._opt.add_alert(
                        f"{name}: retired after {cycles} failed probe cycles"
                        " — remove from registry or fix connectivity",
                    )
                else:
                    self._pool.set_offline(name)
                    self._on_go_offline(name)


class OptimizerPolicy:
    _FLOOR_ALERT_INTERVAL = 60.0

    def __init__(self, optimizer: Optimizer) -> None:
        self._opt = optimizer
        self._last_floor_alert: dict[str | None, float] = {}

    def select(self, candidates: list[LLMConfig], *, operation: str | None) -> LLMConfig | None:
        if not candidates:
            return None
        if random.random() < self._opt.exploration_fraction:  # noqa: S311
            return random.choice(candidates)  # noqa: S311
        gated = [c for c in candidates if self._passes_floor(c, operation)]
        pool = gated if gated else candidates
        if not gated:
            now = time.monotonic()
            if (
                now - self._last_floor_alert.get(operation, float("-inf"))
                >= self._FLOOR_ALERT_INTERVAL
            ):
                self._last_floor_alert[operation] = now
                self._opt.add_alert(
                    f"quality floor {self._opt.usable_rate_floor} dropped all candidates "
                    f"for operation={operation!r}; using score-ranked fallback over all candidates",
                )
        is_background = operation in self._opt.background_operations
        return min(pool, key=lambda c: self._rank_key(c, operation, is_background))

    def _passes_floor(self, config: LLMConfig, operation: str | None) -> bool:
        rate = self._opt.usable_rate(config.name, operation)
        return rate is None or rate >= self._opt.usable_rate_floor

    def _rank_key(
        self,
        config: LLMConfig,
        operation: str | None,
        is_background: bool,
    ) -> tuple:
        rate_val = self._opt.usable_rate(config.name, operation)
        rate = rate_val if rate_val is not None else 0.5
        latency_val = self._opt.mean_latency_ms(config.name, operation)
        latency = latency_val if latency_val is not None else float("inf")
        if is_background:
            return (-rate, latency)
        return (latency, -rate)

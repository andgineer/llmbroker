"""The ``Optimizer`` knob — per-LLM failure bookkeeping, decayed quality aggregates,
and automatic retirement/demotion policy for the pool."""

import logging
import random
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from statistics import NormalDist
from typing import TYPE_CHECKING, Protocol

from llmbroker.models import (
    Alert,
    Call,
    CallStatus,
    LLMConfig,
    LLMMetrics,
    LLMProfile,
    QualitySummary,
)
from llmbroker.protocols.telemetry import QueryableTelemetryProtocol, TelemetryProtocol

if TYPE_CHECKING:
    from llmbroker.broker.pool import LLMPool

logger = logging.getLogger("llmbroker.broker")

_KIND_TRANSPORT = "transport"
_KIND_LATENCY = "latency"
_KIND_QUALITY = "quality"

# Ranking (usable_rate / mean_latency_ms): 80% confidence to resolve a 0.2 gap
# around p=0.5 -> n ~= 1.28^2 * 0.25 / 0.2^2 ~= 10 -> d = (n-1)/(n+1).
_RANK_N = 10
_D_RANK = (_RANK_N - 1) / (_RANK_N + 1)  # 9/11

_CALL_INDEX_CAP = 10_000


def _z_score(confidence: float) -> float:
    """Two-sided z for a given confidence level, e.g. 0.95 -> ~1.96."""
    return NormalDist().inv_cdf((1 + confidence) / 2)


class SelectionPolicy(Protocol):
    def select(self, candidates: list[LLMConfig], *, operation: str | None) -> LLMConfig | None:
        """Pick the best candidate from the currently available list.
        None means no preference — caller may pick any."""


class FirstAvailablePolicy:
    def select(self, candidates: list[LLMConfig], *, operation: str | None) -> LLMConfig | None:  # noqa: ARG002
        return candidates[0] if candidates else None


@dataclass
class Optimizer:
    """Consecutive-failure counter, decayed quality/transport aggregates, and
    retirement/demotion policy for the pool."""

    max_delay: float = 3600.0
    backoff_factor: float = 2.0
    min_sample_count: int = 10
    usable_rate_floor: float = 0.5
    removal_rate_floor: float = 0.15
    exploration_fraction: float = 0.1
    background_operations: frozenset[str] = field(default_factory=frozenset)

    quality_floor: float = 0.3
    quality_margin: float = 0.15
    quality_confidence: float = 0.95
    quality_effective_n: int = 36
    demotion_realert_interval: float = 3600.0

    _pending_alerts: list[Alert] = field(default_factory=list, init=False, repr=False)
    _rl_fail_count: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _ranking: dict[tuple[str, str | None], QualitySummary] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _latency: dict[tuple[str, str | None], QualitySummary] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _quality: dict[tuple[str, str | None], QualitySummary] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _benched: set[str] = field(default_factory=set, init=False, repr=False)

    @property
    def _d_quality(self) -> float:
        n = self.quality_effective_n
        return (n - 1) / (n + 1)

    def rl_fail_count(self, llm_name: str) -> int:
        return self._rl_fail_count.get(llm_name, 0)

    def on_rate_limited(self, llm_name: str) -> None:
        """Increment the consecutive-failure count the router reads for its backoff exponent."""
        self._rl_fail_count[llm_name] = self._rl_fail_count.get(llm_name, 0) + 1

    def on_success(self, llm_name: str) -> None:
        self._rl_fail_count[llm_name] = 0

    def alerts(self) -> list[Alert]:
        result = list(self._pending_alerts)
        self._pending_alerts.clear()
        return result

    def add_alert(self, msg: str) -> None:
        self._pending_alerts.append(Alert(message=msg))

    # ------------------------------------------------------------------
    # Ranking aggregate (transport outcome + latency) — every call
    # ------------------------------------------------------------------

    def _record_transport(self, llm_name: str, operation: str | None, call: Call) -> None:
        key = (llm_name, operation)
        summary = self._ranking.setdefault(key, QualitySummary())
        summary.update(1.0 if call.status == CallStatus.OK else 0.0, _D_RANK)
        if call.status == CallStatus.OK and call.latency_ms is not None:
            lat_summary = self._latency.setdefault(key, QualitySummary())
            lat_summary.update(float(call.latency_ms), _D_RANK)

    def usable_rate(self, llm_name: str, operation: str | None) -> float | None:
        """Jeffreys-smoothed decayed rate. None if fewer than min_sample_count events."""
        summary = self._ranking.get((llm_name, operation))
        if summary is None or summary.count < self.min_sample_count:
            return None
        return (summary.weighted_good + 0.5) / (summary.weight + 1.0)

    def mean_latency_ms(self, llm_name: str, operation: str | None) -> float | None:
        """Decayed mean latency of OK calls; None if no OK call recorded."""
        summary = self._latency.get((llm_name, operation))
        if summary is None or summary.count == 0:
            return None
        return summary.weighted_good / summary.weight

    def should_retire(self, llm_name: str, operation: str | None) -> bool:
        """True once usable_rate has enough samples and sits below removal_rate_floor.

        Distinct from (and stricter than) usable_rate_floor, which only deprioritizes a
        candidate in routing — reusing that floor here would remove a model the instant
        it dips below it, with no margin for the still-useful-as-last-resort case.
        """
        rate = self.usable_rate(llm_name, operation)
        return rate is not None and rate < self.removal_rate_floor

    # ------------------------------------------------------------------
    # Quality aggregate + derived per-operation / global demotions
    # ------------------------------------------------------------------

    def _record_quality(self, llm_name: str, operation: str | None, score: float) -> None:
        key = (llm_name, operation)
        summary = self._quality.setdefault(key, QualitySummary())
        summary.update(score, self._d_quality)

    def reset_quality(self, llm_name: str) -> None:
        """Clear every operation's quality aggregate for this model — a clean trial period.

        Called by ``enable_llm`` so stale evidence cannot immediately re-derive a demotion.
        """
        for key in [k for k in self._quality if k[0] == llm_name]:
            del self._quality[key]

    def set_benched(self, llm_name: str) -> None:
        self._benched.add(llm_name)

    def clear_benched(self, llm_name: str) -> None:
        self._benched.discard(llm_name)

    def wilson_bound(self, llm_name: str, operation: str | None) -> float | None:
        """The Wilson-score upper bound backing ``evaluate_demotions``, for alert messages."""
        summary = self._quality.get((llm_name, operation))
        if summary is None:
            return None
        z = _z_score(self.quality_confidence)
        return summary.wilson_upper(z, min_count=self.quality_effective_n)

    def _demotion_verdicts(self, llm_name: str) -> dict[str | None, bool | None]:
        """Per-operation verdicts: ``True``/``False`` when evidenced, ``None`` when the
        Wilson-score bound could not be computed (insufficient samples)."""
        if llm_name in self._benched:
            return {}
        result: dict[str | None, bool | None] = {}
        for name, operation in self._quality:
            if name != llm_name:
                continue
            upper = self.wilson_bound(name, operation)
            result[operation] = None if upper is None else upper < self.quality_floor
        return result

    def evaluate_demotions(self, llm_name: str) -> dict[str | None, bool]:
        """Per-operation demotion verdicts, derived from the quality aggregate.

        Demoted (for an operation with sufficient evidence) iff the Wilson-score upper
        bound sits below ``quality_floor``. Returns ``{}`` unconditionally when the model
        is manually benched — the manual latch already excludes it; deriving demotions on
        top would be meaningless.
        """
        return {op: bool(verdict) for op, verdict in self._demotion_verdicts(llm_name).items()}

    def is_globally_demoted(self, llm_name: str) -> bool:
        """True iff every operation with sufficient evidence is demoted and at least one exists.

        Operations with insufficient evidence (``None``) are excluded from this check —
        counting them as "not demoted" would let one freshly-tried operation mask an
        otherwise uniformly-bad model, and counting them as "demoted" would flag a model
        that has not actually been shown bad on any operation yet.
        """
        evidenced = [v for v in self._demotion_verdicts(llm_name).values() if v is not None]
        return bool(evidenced) and all(evidenced)

    @property
    def transport_decay(self) -> float:
        """The ranking aggregate's per-event decay constant — for the broker's live sync."""
        return _D_RANK

    @property
    def quality_decay(self) -> float:
        """The quality aggregate's per-event decay constant — for the broker's live sync."""
        return self._d_quality

    # ------------------------------------------------------------------
    # Durable profile snapshot / warm-start
    # ------------------------------------------------------------------

    def to_profile(self, llm_name: str) -> LLMProfile:
        """Snapshot this model's summaries (all kinds) into a durable ``LLMProfile``.

        The manual-bench latch fields are the broker's concern — this only carries
        the learned aggregates.
        """
        stats: dict[str | None, dict[str, QualitySummary]] = {}
        for (name, operation), summary in self._ranking.items():
            if name == llm_name:
                stats.setdefault(operation, {})[_KIND_TRANSPORT] = summary
        for (name, operation), summary in self._latency.items():
            if name == llm_name:
                stats.setdefault(operation, {})[_KIND_LATENCY] = summary
        for (name, operation), summary in self._quality.items():
            if name == llm_name:
                stats.setdefault(operation, {})[_KIND_QUALITY] = summary
        return LLMProfile(stats=stats)

    def load_summaries(self, llm_name: str, profile: LLMProfile) -> None:
        """Warm-start this model's in-memory aggregates from a persisted ``LLMProfile``."""
        for operation, kinds in profile.stats.items():
            if _KIND_TRANSPORT in kinds:
                self._ranking[(llm_name, operation)] = kinds[_KIND_TRANSPORT]
            if _KIND_LATENCY in kinds:
                self._latency[(llm_name, operation)] = kinds[_KIND_LATENCY]
            if _KIND_QUALITY in kinds:
                self._quality[(llm_name, operation)] = kinds[_KIND_QUALITY]


class OptimizerTelemetry:
    """Wraps any TelemetryProtocol and drives Optimizer bookkeeping from the live event stream."""

    def __init__(
        self,
        optimizer: Optimizer,
        inner: TelemetryProtocol,
        pool: "LLMPool",
    ) -> None:
        self._opt = optimizer
        self._inner = inner
        self._pool = pool
        self._call_index: OrderedDict[str, tuple[str, str | None]] = OrderedDict()

    async def record(self, call: Call) -> None:
        try:
            await self._inner.record(call)
        finally:
            self._call_index[call.id] = (call.llm_name, call.operation)
            if len(self._call_index) > _CALL_INDEX_CAP:
                self._call_index.popitem(last=False)
            self._drive_fsm(call)

    def peek_call(self, call_id: str) -> tuple[str, str | None] | None:
        """The ``(name, operation)`` a not-yet-rated ``call_id`` belongs to, or ``None``.

        Exposed for the broker's live profile-sync path, which needs to know where a
        rating is headed *before* ``record_quality`` pops the index entry.
        """
        return self._call_index.get(call_id)

    async def record_quality(self, call_id: str, score: float) -> None:
        await self._inner.record_quality(call_id, score)
        entry = self._call_index.pop(call_id, None)
        if entry is None:
            logger.warning(
                "record_quality: call %s not indexed (aged out or prior process);"
                " quality score not aggregated",
                call_id,
            )
            return
        name, operation = entry
        self._opt._record_quality(name, operation, score)  # noqa: SLF001

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

    def _maybe_retire(self, name: str, operation: str | None) -> None:
        if self._opt.should_retire(name, operation):
            self._pool.drop(name)
            self._opt.add_alert(f"{name}: retired — success rate too low over recent calls")

    def _drive_fsm(self, call: Call) -> None:
        name = call.llm_name
        self._opt._record_transport(name, call.operation, call)  # noqa: SLF001

        if call.status in (CallStatus.RATE_LIMITED, CallStatus.UNAVAILABLE):
            self._opt.on_rate_limited(name)
            self._maybe_retire(name, call.operation)
        elif call.status == CallStatus.OK:
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
            else:
                self._opt.on_rate_limited(name)
                self._maybe_retire(name, call.operation)


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

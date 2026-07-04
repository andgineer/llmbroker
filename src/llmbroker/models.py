"""DTOs, enums, and the shared resource-lifecycle protocol for llmbroker.

Pure data and the one cross-cutting capability protocol. No I/O, no driver
imports — safe to import from anywhere in the package.
"""

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable


class LifecyclePhase(Enum):
    """The FSM label for one LLM's lifecycle, always derived from cooldown_until vs now."""

    AVAILABLE = "available"
    COOLING = "cooling"


_RESERVED_STATE_KEYS = frozenset({"phase", "cooldown_until", "fail_count"})


@dataclass(frozen=True, slots=True)
class LLMState:
    """Snapshot of one LLM's live runtime state, built fresh on each read."""

    phase: LifecyclePhase = LifecyclePhase.AVAILABLE
    cooldown_until: datetime | None = None
    fail_count: int = 0
    extra: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Serialize to a plain, JSON-storable dict.

        >>> s = LLMState(fail_count=2, extra={"probe_attempts": 1})
        >>> d = s.to_dict()
        >>> LLMState.from_dict(d) == s
        True
        >>> LLMState.from_dict({}) == LLMState()
        True
        """
        collision = _RESERVED_STATE_KEYS & self.extra.keys()
        if collision:
            raise ValueError(
                f"LLMState.extra must not contain reserved keys: {sorted(collision)}",
            )
        return {
            "phase": self.phase.value,
            "cooldown_until": self.cooldown_until.isoformat()
            if self.cooldown_until is not None
            else None,
            "fail_count": self.fail_count,
            **self.extra,
        }

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> "LLMState":
        """Deserialize from a plain dict; a missing key falls back to its dataclass default."""
        phase = LifecyclePhase(d["phase"]) if "phase" in d else LifecyclePhase.AVAILABLE
        cooldown_raw = d.get("cooldown_until")
        cooldown_until = (
            datetime.fromisoformat(cooldown_raw) if isinstance(cooldown_raw, str) else None
        )
        fail_count_raw = d.get("fail_count", 0)
        fail_count = fail_count_raw if isinstance(fail_count_raw, int) else 0
        extra = {k: v for k, v in d.items() if k not in _RESERVED_STATE_KEYS}
        return cls(phase=phase, cooldown_until=cooldown_until, fail_count=fail_count, extra=extra)


def reconcile(state: LLMState, now: datetime) -> LLMState:
    """Derive the effective phase from cooldown_until vs now.

    >>> from datetime import UTC, datetime, timedelta
    >>> now = datetime(2030, 1, 1, tzinfo=UTC)
    >>> reconcile(
    ...     LLMState(phase=LifecyclePhase.COOLING, cooldown_until=now + timedelta(days=1)), now
    ... ).phase
    <LifecyclePhase.COOLING: 'cooling'>
    """
    cooldown_until = state.cooldown_until
    if cooldown_until is not None and cooldown_until > now:
        phase = LifecyclePhase.COOLING
    else:
        phase = LifecyclePhase.AVAILABLE
        cooldown_until = None
    return replace(state, phase=phase, cooldown_until=cooldown_until)


@dataclass(slots=True)
class QualitySummary:
    """Exponentially decayed weighted-proportion counter over per-event outcomes.

    Decay is applied per event, not per elapsed time: ``weight``/``weighted_good``/
    ``weight_sq`` are folded on every ``update()`` call, while ``count`` is a plain,
    un-decayed integer used as the only trust gate — ``weight`` asymptotically
    approaches but never reaches ``1 / (1 - decay)`` and must never be compared
    against a threshold directly.
    """

    weight: float = 0.0
    weighted_good: float = 0.0
    weight_sq: float = 0.0
    count: int = 0

    def update(self, value: float, decay: float) -> None:
        """Fold one new event (outcome ``value``) into the aggregate.

        >>> s = QualitySummary()
        >>> s.update(1.0, 0.5)
        >>> s.update(0.0, 0.5)
        >>> round(s.weight, 4), round(s.weighted_good, 4), s.count
        (1.5, 0.5, 2)
        """
        self.weight = self.weight * decay + 1.0
        self.weighted_good = self.weighted_good * decay + value
        self.weight_sq = self.weight_sq * decay * decay + 1.0
        self.count += 1

    @property
    def n_eff(self) -> float:
        """Kish effective sample size ``weight² / weight_sq``; ``0.0`` when empty.

        >>> s = QualitySummary()
        >>> s.update(1.0, 0.9)
        >>> s.n_eff
        1.0
        """
        if self.weight_sq <= 0.0:
            return 0.0
        return self.weight * self.weight / self.weight_sq

    def wilson_upper(self, z: float, *, min_count: int) -> float | None:
        """Wilson-score upper bound of ``weighted_good / weight`` at confidence ``z``.

        Computed with the exact effective sample size ``n_eff``, not the asymptotic
        weight ceiling. Returns ``None`` when ``count < min_count`` — insufficient
        evidence to judge, regardless of how close ``weight`` sits to its ceiling.
        ``min_count`` is required: callers supply their own trust threshold, keeping
        this aggregate free of any caller-specific configuration.
        """
        if self.count < min_count or self.weight <= 0.0:
            return None
        n = self.n_eff
        if n <= 0.0:
            return None
        p = self.weighted_good / self.weight
        z2 = z * z
        center = p + z2 / (2 * n)
        margin = z * ((p * (1 - p) / n) + z2 / (4 * n * n)) ** 0.5
        denom = 1 + z2 / n
        return (center + margin) / denom

    def to_dict(self) -> dict[str, object]:
        return {
            "weight": self.weight,
            "weighted_good": self.weighted_good,
            "weight_sq": self.weight_sq,
            "count": self.count,
        }

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> "QualitySummary":
        return cls(
            weight=float(d.get("weight", 0.0)),  # type: ignore[arg-type]
            weighted_good=float(d.get("weighted_good", 0.0)),  # type: ignore[arg-type]
            weight_sq=float(d.get("weight_sq", 0.0)),  # type: ignore[arg-type]
            count=int(d.get("count", 0)),  # type: ignore[arg-type]
        )


_RESERVED_PROFILE_KEYS = frozenset({"stats", "benched", "benched_since", "benched_reason"})


@dataclass(frozen=True, slots=True)
class LLMProfile:
    """The durable learned half of one catalog entry.

    Per-``(operation, kind)`` decayed summaries (``kind`` ∈ quality / transport /
    latency) plus the manual-bench latch. Owned by the optimizer (and, for the
    latch, the admin); the seed path never writes this. Demotions are derived from
    ``stats`` at read time and are deliberately not stored here.
    """

    stats: dict[str | None, dict[str, QualitySummary]] = field(default_factory=dict)
    benched: bool = False
    benched_since: datetime | None = None
    benched_reason: str | None = None
    extra: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        """Serialize to a plain, JSON-storable dict.

        >>> from datetime import UTC
        >>> p = LLMProfile(
        ...     stats={
        ...         "summarize": {"quality": QualitySummary(1.0, 0.8, 1.0, 1)},
        ...         None: {"transport": QualitySummary(2.0, 1.5, 1.5, 2)},
        ...     },
        ...     benched=True,
        ...     benched_since=datetime(2030, 1, 1, tzinfo=UTC),
        ...     benched_reason="manual review",
        ... )
        >>> LLMProfile.from_dict(p.to_dict()) == p
        True
        >>> LLMProfile.from_dict({}) == LLMProfile()
        True
        >>> LLMProfile.from_dict({"future_field": 1}).extra
        {'future_field': 1}
        """
        collision = _RESERVED_PROFILE_KEYS & self.extra.keys()
        if collision:
            raise ValueError(
                f"LLMProfile.extra must not contain reserved keys: {sorted(collision)}",
            )
        stats_list = [
            {"operation": operation, "kind": kind, **summary.to_dict()}
            for operation, kinds in self.stats.items()
            for kind, summary in kinds.items()
        ]
        return {
            "stats": stats_list,
            "benched": self.benched,
            "benched_since": self.benched_since.isoformat()
            if self.benched_since is not None
            else None,
            "benched_reason": self.benched_reason,
            **self.extra,
        }

    @classmethod
    def from_dict(cls, d: dict[str, object]) -> "LLMProfile":
        """Deserialize from a plain dict; a missing key falls back to its dataclass default."""
        stats: dict[str | None, dict[str, QualitySummary]] = {}
        raw_stats = d.get("stats")
        if isinstance(raw_stats, list):
            for entry in raw_stats:
                if not isinstance(entry, dict):
                    continue
                kind = entry.get("kind")
                if not isinstance(kind, str):
                    continue
                operation = entry.get("operation")
                op_key = operation if isinstance(operation, str) else None
                stats.setdefault(op_key, {})[kind] = QualitySummary.from_dict(entry)
        benched_since_raw = d.get("benched_since")
        benched_since = (
            datetime.fromisoformat(benched_since_raw)
            if isinstance(benched_since_raw, str)
            else None
        )
        benched_reason_raw = d.get("benched_reason")
        benched_reason = benched_reason_raw if isinstance(benched_reason_raw, str) else None
        extra = {k: v for k, v in d.items() if k not in _RESERVED_PROFILE_KEYS}
        return cls(
            stats=stats,
            benched=bool(d.get("benched", False)),
            benched_since=benched_since,
            benched_reason=benched_reason,
            extra=extra,
        )


class EffortLevel(Enum):
    """How hard an api_key_ref is to obtain, easiest first.

    Declaration order is the onboarding sort key: ``list(EffortLevel).index(...)``.
    """

    OAUTH = "oauth"
    SIGNUP = "signup"
    VERIFY = "verify"
    CONSOLE = "console"
    WAITLIST = "waitlist"


class ValueLevel(Enum):
    """How good the best model an api_key_ref unlocks is, most desirable first."""

    HIGH = "high"
    GOOD = "good"
    NICHE = "niche"


@dataclass(frozen=True, slots=True)
class KeyInfo:
    """Per-provider onboarding metadata for one ``api_key_ref``.

    ``rate_limit`` is not here — it's per-model, not per-key.
    """

    api_key_ref: str
    effort: EffortLevel | None
    value: ValueLevel | None
    help: str


class Origin(Enum):
    """Provenance of a catalog entry — who is authoritative for its static fields."""

    PRESET = "preset"
    USER = "user"


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """Pure stored config for one LLM — no secret, safe to expose.

    ``origin`` and ``deprecated`` are the two curated markers of the static catalog
    half (see architecture.md#columns-vs-json): written only by the seed path and
    ``broker.add`` — never by the optimizer.
    """

    name: str
    base_url: str
    model: str
    api_key_ref: str
    parallel: int | None = None
    origin: Origin | None = None
    deprecated: bool = False

    def to_metadata(self) -> dict[str, object]:
        """Structured optional config, serialized for the registry's JSON column.

        >>> LLMConfig(name="g", base_url="https://x/v1", model="m", api_key_ref="K").to_metadata()
        {}
        """
        metadata: dict[str, object] = {}
        if self.parallel is not None:
            metadata["parallel"] = self.parallel
        if self.origin is not None:
            metadata["origin"] = self.origin.value
        if self.deprecated:
            metadata["deprecated"] = True
        return metadata

    @classmethod
    def from_metadata(
        cls,
        *,
        name: str,
        base_url: str,
        model: str,
        api_key_ref: str,
        metadata: dict[str, object] | None,
    ) -> "LLMConfig":
        """Reconstruct from the core columns plus the JSON ``metadata`` blob."""
        metadata = metadata or {}
        raw_parallel = metadata.get("parallel")
        parallel = raw_parallel if isinstance(raw_parallel, int) else None
        raw_origin = metadata.get("origin")
        origin = Origin(raw_origin) if isinstance(raw_origin, str) else None
        return cls(
            name=name,
            base_url=base_url,
            model=model,
            api_key_ref=api_key_ref,
            parallel=parallel,
            origin=origin,
            deprecated=bool(metadata.get("deprecated", False)),
        )


class CallStatus(Enum):
    OK = "ok"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class Usage:
    """Resource use the provider reported for one call."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    extra: dict[str, int] | None = None


@dataclass(frozen=True, slots=True)
class Call:
    """One telemetry record. ``id`` is the uuid record_quality updates by."""

    id: str
    llm_name: str
    operation: str | None
    trace_id: str | None
    status: CallStatus
    http_status: int | None = None
    latency_ms: int | None = None
    error_detail: str | None = None
    usage: Usage | None = None
    quality_score: float | None = None
    user_id: int | str | None = None


@dataclass(frozen=True, slots=True)
class LLMMetrics:
    """Per-LLM admin read-model derived from Call rows."""

    call_count: int
    last_status: CallStatus | None
    last_at: datetime | None


@dataclass(frozen=True, slots=True)
class LLMSnapshot:
    """Frozen point-in-time materialization of one LLM (config + state + metrics)."""

    config: LLMConfig
    state: LLMState
    metrics: LLMMetrics | None


@dataclass(frozen=True, slots=True)
class Alert:
    """One human-actionable signal from the Optimizer."""

    message: str = field(default="")


class SeedPolicy(Enum):
    MIRROR = "mirror"
    ADD = "add"
    IF_EMPTY = "if_empty"
    SYNC = "sync"


@runtime_checkable
class AsyncResourceProtocol(Protocol):
    """Lifecycle capability for any backend that holds an open resource.

    Orthogonal to a backend's data contract. ``aclose()`` is idempotent.
    """

    async def aclose(self) -> None: ...


def check_user_id(user_id: int | str | None) -> None:
    """Reject an empty-string ``user_id`` (use ``None`` for unscoped).

    Shared by every storage backend so scoping behaves identically across them.

    >>> check_user_id(None)
    >>> check_user_id(42)
    >>> check_user_id("")
    Traceback (most recent call last):
    ...
    ValueError: user_id must not be empty string; use None for unscoped
    """
    if user_id == "":
        raise ValueError("user_id must not be empty string; use None for unscoped")

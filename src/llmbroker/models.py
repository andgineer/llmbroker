"""DTOs, enums, and the shared resource-lifecycle protocol for llmbroker.

Pure data and the one cross-cutting capability protocol. No I/O, no driver
imports — safe to import from anywhere in the package.
"""

import hashlib
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


@dataclass(frozen=True, slots=True)
class KeyInfo:
    """Per-provider onboarding metadata for one ``api_key_ref``: a help blurb plus
    a free-form passthrough of whatever else the TOML ``[keys.REF]`` section holds —
    llmbroker has no taxonomy opinion on it."""

    api_key_ref: str
    help: str
    extra: dict[str, str]


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """Pure stored config for one LLM — no secret, safe to expose.

    The registry is a pure mirror of the preset (see ``sync``): nothing else
    writes it, so there is no provenance/curation marker to carry here.
    """

    name: str
    base_url: str
    model: str
    api_key_ref: str
    parallel: int | None = None

    def to_metadata(self) -> dict[str, object]:
        """Structured optional config, serialized for the registry's JSON column.

        >>> LLMConfig(name="g", base_url="https://x/v1", model="m", api_key_ref="K").to_metadata()
        {}
        """
        metadata: dict[str, object] = {}
        if self.parallel is not None:
            metadata["parallel"] = self.parallel
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
        return cls(
            name=name,
            base_url=base_url,
            model=model,
            api_key_ref=api_key_ref,
            parallel=parallel,
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
    """One append-only journal record: a call attempt (``kind="call"``) or a
    self-contained quality rating (``kind="quality"``), interleaved in one stream.

    A quality record fills only ``llm_name``, ``operation``, ``quality_score``,
    ``ts``, and optionally ``call_id`` (an opaque host-UI passthrough — never
    joined against the call row it rates); ``status`` is ``None`` exactly on
    quality records.
    """

    id: str
    llm_name: str
    operation: str | None
    trace_id: str | None
    status: CallStatus | None
    kind: str = "call"
    ts: datetime | None = None
    http_status: int | None = None
    latency_ms: int | None = None
    error_detail: str | None = None
    usage: Usage | None = None
    quality_score: float | None = None
    call_id: str | None = None
    scope: str | None = None
    cooldown_until: datetime | None = None
    key_hash: str | None = None


def key_hash(secret: str) -> str:
    """Short digest of a resolved key value — the quota-scope identity for shared
    cooldowns and dead-key drops (never the key itself).

    >>> key_hash("sk-abc") == key_hash("sk-abc")
    True
    >>> len(key_hash("sk-abc"))
    12
    """
    return hashlib.sha256(secret.encode()).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class LLMMetrics:
    """Per-LLM admin read-model derived from Call rows."""

    call_count: int
    last_status: CallStatus | None
    last_at: datetime | None


@dataclass(frozen=True, slots=True)
class LLMSnapshot:
    """Frozen point-in-time materialization of one LLM: raw facts, no status enum
    or precedence rule — the host derives whatever presentation it wants."""

    config: LLMConfig
    disabled: bool
    has_key: bool
    cooldown_until: datetime | None
    demoted_operations: tuple[str, ...]
    metrics: LLMMetrics | None


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

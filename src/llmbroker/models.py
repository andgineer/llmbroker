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


@dataclass(frozen=True, slots=True)
class RateLimit:
    """Optional per-LLM rate ceilings; any field left ``None`` is not enforced."""

    rpm: int | None = None
    rpd: int | None = None
    tpm: int | None = None
    tpd: int | None = None


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


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """Pure stored config for one LLM — no secret, safe to expose."""

    name: str
    base_url: str
    model: str
    api_key_ref: str
    rate_limit: RateLimit | None = None

    def to_metadata(self) -> dict[str, object]:
        """Structured optional config, serialized for the registry's JSON column.

        >>> LLMConfig(name="g", base_url="https://x/v1", model="m", api_key_ref="K").to_metadata()
        {}
        """
        if self.rate_limit is None:
            return {}
        return {
            "rate_limit": {
                "rpm": self.rate_limit.rpm,
                "rpd": self.rate_limit.rpd,
                "tpm": self.rate_limit.tpm,
                "tpd": self.rate_limit.tpd,
            },
        }

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
        raw_rate_limit = metadata.get("rate_limit")
        rate_limit = RateLimit(**raw_rate_limit) if isinstance(raw_rate_limit, dict) else None
        return cls(
            name=name,
            base_url=base_url,
            model=model,
            api_key_ref=api_key_ref,
            rate_limit=rate_limit,
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

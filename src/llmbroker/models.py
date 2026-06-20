"""DTOs, enums, and the shared resource-lifecycle protocol for llmbroker.

Pure data and the one cross-cutting capability protocol. No I/O, no driver
imports — safe to import from anywhere in the package.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal, Protocol, runtime_checkable


class LifecyclePhase(Enum):
    """The FSM label for one LLM's lifecycle.

    AVAILABLE/COOLING are derived from cooldown_until vs now. OFFLINE/PROBING
    are set by the Optimizer (P4) and never occur in P1.
    """

    AVAILABLE = "available"
    COOLING = "cooling"
    OFFLINE = "offline"
    PROBING = "probing"


@dataclass(frozen=True, slots=True)
class LLMState:
    """Snapshot of one LLM's live runtime state, built fresh on each read."""

    phase: LifecyclePhase = LifecyclePhase.AVAILABLE
    cooldown_until: datetime | None = None
    fail_count: int = 0


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """Pure stored config for one LLM — no secret, safe to expose."""

    name: str
    base_url: str
    model: str
    api_key_ref: str


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
    """One human-actionable signal from the Optimizer (P4 placeholder)."""

    message: str = field(default="")


SyncPolicy = Literal["mirror", "add", "if_empty"]


@runtime_checkable
class AsyncResourceProtocol(Protocol):
    """Lifecycle capability for any port that holds an open resource.

    Orthogonal to a port's data contract. ``aclose()`` is idempotent.
    """

    async def aclose(self) -> None: ...

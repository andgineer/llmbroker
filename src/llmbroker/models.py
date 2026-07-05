"""DTOs, enums, and the shared resource-lifecycle protocol for llmbroker.

Pure data and the one cross-cutting capability protocol. No I/O, no driver
imports — safe to import from anywhere in the package.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable


class LifecyclePhase(Enum):
    """The FSM label for one LLM's lifecycle, always derived from cooldown_until vs now."""

    AVAILABLE = "available"
    COOLING = "cooling"


@dataclass(frozen=True, slots=True)
class LLMState:
    """Snapshot of one LLM's live runtime state, built fresh on each read."""

    phase: LifecyclePhase = LifecyclePhase.AVAILABLE
    cooldown_until: datetime | None = None
    fail_count: int = 0


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
    or precedence rule — the host derives whatever presentation it wants.

    ``demoted_operations`` may contain ``None``: the bucket for calls made without
    an ``operation=`` label.
    """

    config: LLMConfig
    disabled: bool
    has_key: bool
    cooldown_until: datetime | None
    demoted_operations: tuple[str | None, ...]
    metrics: LLMMetrics | None


@runtime_checkable
class AsyncResourceProtocol(Protocol):
    """Lifecycle capability for any backend that holds an open resource.

    Orthogonal to a backend's data contract. ``aclose()`` is idempotent.
    """

    async def aclose(self) -> None: ...

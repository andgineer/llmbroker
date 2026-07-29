"""DTOs, enums, and the shared resource-lifecycle protocol for llmbroker.

Pure data and the one cross-cutting capability protocol. No I/O, no driver
imports — safe to import from anywhere in the package.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
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

    Two orthogonal flags carry the entry's role:

    * ``pooled`` — pool membership: a ``pooled=False`` entry is left out of the
      routed pool (no failover onto it) yet stays reachable directly by name.
    * ``custom`` — provenance: a ``custom=True`` entry is user-owned, not part of
      the curated preset, so ``sync`` never prunes it. Independent of ``pooled``
      — a custom entry may be pooled (a user's own extra pool model) or not (a
      direct-only model such as a paid one).
    """

    name: str
    base_url: str
    model: str
    api_key_ref: str
    parallel: int | None = None
    pooled: bool = True
    custom: bool = False

    def to_metadata(self) -> dict[str, object]:
        """Structured optional config, serialized for the registry's JSON column.

        Only non-default values are stored, so a plain pooled config stays empty.

        >>> LLMConfig(name="g", base_url="https://x/v1", model="m", api_key_ref="K").to_metadata()
        {}
        >>> paid = LLMConfig(name="g", base_url="u", model="m", api_key_ref="K", pooled=False)
        >>> paid.to_metadata()
        {'pool': False}
        """
        metadata: dict[str, object] = {}
        if self.parallel is not None:
            metadata["parallel"] = self.parallel
        if not self.pooled:
            metadata["pool"] = False
        if self.custom:
            metadata["custom"] = True
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
        raw_pool = metadata.get("pool")
        pooled = raw_pool if isinstance(raw_pool, bool) else True
        raw_custom = metadata.get("custom")
        custom = raw_custom if isinstance(raw_custom, bool) else False
        return cls(
            name=name,
            base_url=base_url,
            model=model,
            api_key_ref=api_key_ref,
            parallel=parallel,
            pooled=pooled,
            custom=custom,
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


def to_utc(value: datetime, field: str) -> datetime:
    """Pin an instant to UTC; refuse a naive one rather than guess its zone.

    >>> from datetime import timedelta, timezone
    >>> to_utc(datetime(2030, 1, 1, 5, tzinfo=timezone(timedelta(hours=5))), "since")
    datetime.datetime(2030, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)
    """
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware, e.g. datetime.now(UTC)")
    return value.astimezone(UTC)


def with_utc_timestamps(call: "Call") -> "Call":
    """Stamp an unset ``ts`` and pin both journal instants to UTC.

    The write side of the same rule the read bound follows: these two fields are
    what the journal orders, windows, and expires by.
    """
    ts = to_utc(call.ts, "Call.ts") if call.ts is not None else datetime.now(UTC)
    cooldown = (
        to_utc(call.cooldown_until, "Call.cooldown_until")
        if call.cooldown_until is not None
        else None
    )
    return replace(call, ts=ts, cooldown_until=cooldown)


def check_limit(limit: int) -> None:
    """Reject a non-positive journal read limit — backends disagree on what one
    means, and pymongo reads ``limit=0`` as *no limit*."""
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")


@dataclass(frozen=True, slots=True)
class LLMMetrics:
    """Per-LLM admin read-model derived from Call rows."""

    call_count: int
    last_status: CallStatus | None
    last_at: datetime | None


@dataclass(frozen=True, slots=True)
class LLMStats:
    """Per-LLM aggregate of call records over a time window.

    ``by_status`` holds only statuses actually seen, so "how many were not OK" is
    a subtraction from ``total``, not an assumption about the enum's shape.
    """

    total: int
    by_status: Mapping[CallStatus, int]
    first_at: datetime | None
    last_at: datetime | None
    last_status: CallStatus | None


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

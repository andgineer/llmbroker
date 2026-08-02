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

    Nothing but ``sync`` writes the registry, and which entries a sync keeps is
    recomputed every time — so there is no retention or curation marker here.

    Two orthogonal flags carry the entry's role:

    * ``pooled`` — pool membership: a ``pooled=False`` entry is left out of the
      routed pool (no failover onto it) yet stays reachable directly by name.
    * ``custom`` — provenance: a ``custom=True`` entry is user-owned, not part of
      the curated preset, so ``sync`` never prunes it. Independent of ``pooled``
      — a custom entry may be pooled (a user's own extra pool model) or not (a
      direct-only model such as a paid one).

    ``alias`` is the entry's eternal handle, set only on custom entries: ``name``
    carries the model version and is rewritten by a catalog refresh, the alias
    never is.
    """

    name: str
    base_url: str
    model: str
    api_key_ref: str
    parallel: int | None = None
    pooled: bool = True
    custom: bool = False
    alias: str | None = None

    def to_metadata(self) -> dict[str, object]:
        """Structured optional config, serialized for the registry's JSON column.

        Only non-default values are stored, so a plain pooled config stays empty.

        >>> LLMConfig(name="g", base_url="https://x/v1", model="m", api_key_ref="K").to_metadata()
        {}
        >>> paid = LLMConfig(name="g", base_url="u", model="m", api_key_ref="K", pooled=False)
        >>> paid.to_metadata()
        {'pool': False}
        >>> followed = LLMConfig(name="g", base_url="u", model="m", api_key_ref="K", alias="opus")
        >>> followed.to_metadata()
        {'alias': 'opus'}
        """
        metadata: dict[str, object] = {}
        if self.parallel is not None:
            metadata["parallel"] = self.parallel
        if not self.pooled:
            metadata["pool"] = False
        if self.custom:
            metadata["custom"] = True
        if self.alias is not None:
            metadata["alias"] = self.alias
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
        raw_alias = metadata.get("alias")
        alias = raw_alias if isinstance(raw_alias, str) else None
        return cls(
            name=name,
            base_url=base_url,
            model=model,
            api_key_ref=api_key_ref,
            parallel=parallel,
            pooled=pooled,
            custom=custom,
            alias=alias,
        )


@dataclass(frozen=True, slots=True)
class PendingKey:
    """One ``api_key_ref`` a synced lineup wants and the secrets store does not have,
    with the entries it holds back inactive until it resolves."""

    api_key_ref: str
    help: str
    entry_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SyncReport:
    """What one sync did, as raw facts — no severity verdict, the host derives that.

    ``kept`` names entries the new lineup dropped that no arrival paid to remove;
    they stay callable and are recomputed on every sync, never stored.
    """

    source: str
    applied: bool
    added: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()
    pending_keys: tuple[PendingKey, ...] = ()
    active_before: int = 0
    active_after: int = 0

    def unlocking_refs(self) -> tuple[str, ...]:
        """The refs whose keys would pay for removing the ``kept`` entries: the
        arrivals nothing could be spent on because their own key is missing."""
        added = set(self.added)
        return tuple(
            pending.api_key_ref
            for pending in self.pending_keys
            if added.intersection(pending.entry_names)
        )

    def _kept_line(self) -> str:
        subject = "it" if len(self.kept) == 1 else "them"
        refs = self.unlocking_refs()
        if not refs:
            tail = f"upstream dropped {subject} and nothing arrived to replace {subject}"
        else:
            setting = refs[0] if len(refs) == 1 else f"any of {', '.join(refs)}"
            tail = (
                f"upstream dropped {subject} and no replacement is usable;"
                f" set {setting} and the next sync removes {subject}"
            )
        return f"  kept: {', '.join(self.kept)} — {tail}"

    def __str__(self) -> str:
        verb = "applied" if self.applied else "refused"
        lines = [
            f"sync {self.source}: {verb}"
            f" — {self.active_before} -> {self.active_after} entries with a key",
        ]
        for label, names in (
            ("added", self.added),
            ("updated", self.updated),
            ("removed", self.removed),
        ):
            if names:
                lines.append(f"  {label}: {', '.join(names)}")
        if self.kept:
            lines.append(self._kept_line())
        for pending in self.pending_keys:
            lines.append(
                f"  pending key {pending.api_key_ref}"
                f" — holds back {', '.join(pending.entry_names)}",
            )
            lines.extend(f"      {line}" for line in pending.help.splitlines() if line.strip())
        if len(lines) == 1:
            lines.append("  no changes")
        return "\n".join(lines)


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


def check_score(score: float) -> None:
    """Reject a quality score outside ``[0, 1]`` — the Wilson bound the optimizer
    derives from the window is only defined on that interval."""
    if not 0.0 <= score <= 1.0:
        raise ValueError(f"quality score must be within [0.0, 1.0], got {score}")


def check_unique_aliases(configs: "list[LLMConfig]") -> None:
    """Reject a registry whose aliases do not name exactly one entry each.

    Enforced by every registry on read, not just the one that parses a file: a
    lookup by alias returns the first match, so a duplicate silently resolves to
    one of two models instead of raising.
    """
    seen: set[str] = set()
    for cfg in configs:
        if cfg.alias is None:
            continue
        if cfg.alias in seen:
            raise ValueError(
                f"Registry: duplicate alias {cfg.alias!r} — an alias names exactly one entry",
            )
        seen.add(cfg.alias)


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

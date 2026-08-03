"""DTOs, enums, and the shared resource-lifecycle protocol for llmbroker.

Pure data and the one cross-cutting capability protocol. No I/O, no driver
imports — safe to import from anywhere in the package.
"""

import hashlib
import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import Enum
from types import MappingProxyType
from typing import Protocol, runtime_checkable

logger = logging.getLogger("llmbroker.registry")

_NO_KEY_HELP: Mapping[str, str] = MappingProxyType({})

# Two providers is what "failover" means: one is a single quota with nothing to
# fall back to. It describes the feature, not a tuning knob.
_MIN_USABLE_PROVIDERS = 2


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


def _weight_from_metadata(raw: object, name: str) -> float:
    """Read a stored weight, clamping instead of raising: a malformed row in a shared
    database must not take a running broker down at pool build. The file parser, where
    a human is looking, raises instead."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        if raw is not None:
            logger.warning("entry %s: ignoring non-numeric weight %r — using 0.0", name, raw)
        return 0.0
    weight = min(1.0, max(0.0, float(raw)))
    if weight != raw:
        logger.warning("entry %s: weight %r out of [0.0, 1.0] — clamped to %s", name, raw, weight)
    return weight


@dataclass(frozen=True, slots=True)
class LLMConfig:
    """Pure stored config for one LLM — no secret, safe to expose.

    Nothing but ``sync`` writes the registry, and which entries a sync keeps is
    recomputed every time — so there is no retention or curation marker here.

    ``custom`` carries the entry's whole role. A ``custom=True`` entry is
    user-owned: ``sync`` never prunes it, the router never reaches it, and it is
    called by name through ``direct``. Everything else is the curated pool, which
    takes no user entries — so pool membership is not a field, it is ``not
    custom``.

    ``alias`` is the entry's eternal handle, set only on custom entries: ``name``
    carries the model version and is rewritten by a catalog refresh, the alias
    never is.

    ``weight`` is a curated prior on the quality rating this entry is expected to
    earn, on the same ``0..1`` scale as a host rating — not a routing hard-order.
    """

    name: str
    base_url: str
    model: str
    api_key_ref: str
    parallel: int | None = None
    custom: bool = False
    alias: str | None = None
    weight: float = 0.0

    def to_metadata(self) -> dict[str, object]:
        """Structured optional config, serialized for the registry's JSON column.

        Only non-default values are stored, so a plain pool config stays empty.

        >>> LLMConfig(name="g", base_url="https://x/v1", model="m", api_key_ref="K").to_metadata()
        {}
        >>> followed = LLMConfig(name="g", base_url="u", model="m", api_key_ref="K", alias="opus")
        >>> followed.to_metadata()
        {'alias': 'opus'}
        >>> LLMConfig(name="g", base_url="u", model="m", api_key_ref="K", weight=0.7).to_metadata()
        {'weight': 0.7}
        """
        metadata: dict[str, object] = {}
        if self.parallel is not None:
            metadata["parallel"] = self.parallel
        if self.custom:
            metadata["custom"] = True
        if self.alias is not None:
            metadata["alias"] = self.alias
        if self.weight:
            metadata["weight"] = self.weight
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
            custom=custom,
            alias=alias,
            weight=_weight_from_metadata(metadata.get("weight"), name),
        )


@dataclass(frozen=True, slots=True)
class PendingKey:
    """One ``api_key_ref`` a synced lineup wants and the secrets store does not have,
    with the entries it holds back inactive until it resolves."""

    api_key_ref: str
    help: str
    entry_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DeclaredModels:
    """What ``direct=`` resolved to: the entries, and where to get the keys they want.

    The help travels with the entries because nothing stores a declared model —
    there is no registry row for a later read to recover it from.
    """

    configs: tuple[LLMConfig, ...] = ()
    # Factory, not a plain default: mappingproxy is unhashable before 3.12, and
    # dataclasses reject an unhashable default outright.
    key_help: Mapping[str, str] = field(default_factory=lambda: _NO_KEY_HELP)


@dataclass(frozen=True, slots=True)
class Retirement:
    """One entry a sync deleted on the strength of this installation's own journal,
    carrying the evidence: the permanent failure that condemned it and how far back
    that failure runs in the window that was read."""

    name: str
    http_status: int | None = None
    since: datetime | None = None


@dataclass(frozen=True, slots=True)
class SyncReport:
    """What one sync did, as raw facts — no severity verdict, the host derives that.

    ``kept`` names entries whose provider the arriving lineup no longer carries and
    that this installation can still call; they keep routing and are recomputed on
    every sync, never stored. ``keys_visible`` says whether a missing key was
    evidence at all at this merge site, and ``keys_scoped`` which of the two ways
    it was not.
    """

    source: str
    applied: bool
    added: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()
    kept_refs: tuple[str, ...] = ()
    retired: tuple[Retirement, ...] = ()
    orphan_refs: tuple[str, ...] = ()
    pending_keys: tuple[PendingKey, ...] = ()
    active_before: int = 0
    active_after: int = 0
    keys_visible: bool = True
    keys_scoped: bool = False

    def _kept_line(self) -> str:
        subject, verb = ("it", "stays") if len(self.kept) == 1 else ("they", "stay")
        refs = ", ".join(self.kept_refs) or "their provider"
        head = f"the lineup no longer carries {refs}"
        if self.keys_visible:
            held = "a key for it" if len(self.kept_refs) == 1 else "keys for them"
            why = f"{head} and this installation has {held}, so {subject} {verb}"
        elif self.keys_scoped:
            why = (
                f"{head}, and keys are per user here so a missing one proves nothing"
                f" — {subject} {verb}"
            )
        else:
            why = (
                f"{head}, and no key resolved here at all so these are not the keys this"
                f" lineup runs on — {subject} {verb}"
            )
        return f"  kept: {', '.join(self.kept)} — {why}"

    def _retired_lines(self) -> list[str]:
        """One line each: a sync deleting an entry has to show its evidence, or an
        admin cannot check the verdict without going to the journal themselves."""
        lines = []
        for item in self.retired:
            status = item.http_status or "a permanent failure"
            since = f" since {item.since:%Y-%m-%d}" if item.since is not None else ""
            lines.append(
                f"  retired: {item.name} — {status}{since}, no successful call since;"
                " the lineup dropped it too",
            )
        return lines

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
        lines.extend(self._retired_lines())
        if self.kept:
            lines.append(self._kept_line())
        for ref in self.orphan_refs:
            lines.append(
                f"  unused key {ref} — nothing here uses it any more;"
                " revoke it at the provider if you do not need it",
            )
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


def check_weight(weight: float) -> None:
    """Reject a curated weight outside ``[0, 1]`` — it is a prior on the same scale as
    a host quality rating, and blends with one."""
    if not 0.0 <= weight <= 1.0:
        raise ValueError(f"weight must be within [0.0, 1.0], got {weight}")


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


@dataclass(frozen=True, slots=True)
class PoolHealth:
    """How much of the pool can actually serve a request, by provider.

    The unit is the ``api_key_ref``: two entries on one ref are one quota and one
    failure domain. ``missing_keys`` names only refs no managed entry can use.
    """

    providers_usable: int = 0
    providers_total: int = 0
    missing_keys: tuple[PendingKey, ...] = ()

    @property
    def degraded(self) -> bool:
        """One usable provider is a single quota with nothing to fail over to. A
        registry that pools nothing has no pool to degrade."""
        return bool(self.providers_total) and self.providers_usable < _MIN_USABLE_PROVIDERS


@dataclass(frozen=True, slots=True)
class PoolSnapshot(Mapping[str, LLMSnapshot]):
    """Point-in-time view of the whole pool. Iterate it like a dict of
    ``name -> LLMSnapshot``; the properties describe the pool as a whole and come
    from the same measurement the degradation alarm uses."""

    _llms: Mapping[str, LLMSnapshot]
    _health: PoolHealth
    _direct_missing_keys: tuple[PendingKey, ...] = ()

    @property
    def providers_usable(self) -> int:
        return self._health.providers_usable

    @property
    def providers_total(self) -> int:
        return self._health.providers_total

    @property
    def missing_keys(self) -> tuple[PendingKey, ...]:
        return self._health.missing_keys

    @property
    def direct_missing_keys(self) -> tuple[PendingKey, ...]:
        """Refs the host's own ``direct``-reachable models want and cannot resolve.

        Kept apart from ``missing_keys``: those count the pool's failover capacity,
        and a model that is never routed can neither degrade it nor be repaired by
        it. A UI still has to be able to say which key is missing and where it
        comes from, which is the whole of what this carries.
        """
        return self._direct_missing_keys

    @property
    def degraded(self) -> bool:
        return self._health.degraded

    def __getitem__(self, name: str) -> LLMSnapshot:
        return self._llms[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._llms)

    def __len__(self) -> int:
        return len(self._llms)


@runtime_checkable
class AsyncResourceProtocol(Protocol):
    """Lifecycle capability for any backend that holds an open resource.

    Orthogonal to a backend's data contract. ``aclose()`` is idempotent.
    """

    async def aclose(self) -> None: ...

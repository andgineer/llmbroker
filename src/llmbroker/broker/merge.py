"""The merge: an arriving lineup becomes the lineup this installation follows. One
decision site for a file and a database alike; see ``rules/sync-merge.md``."""

import asyncio
import logging
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType

from llmbroker.broker.aliases import AliasRefresh, AliasTarget, refresh_alias_configs
from llmbroker.broker.keys import KeyEvidence, KeyProbe
from llmbroker.broker.presets import PRESET_NAME_RE, PresetSource
from llmbroker.exceptions import SyncRefusedError
from llmbroker.http_status import is_permanent
from llmbroker.models import (
    Call,
    CallStatus,
    KeyInfo,
    Lineup,
    LLMConfig,
    PendingKey,
    Retirement,
    SyncReport,
)
from llmbroker.protocols.store import QueryableStoreProtocol, StoreProtocol
from llmbroker.standalone.registry import ALIAS_NAME_HINT, parse_lineup

logger = logging.getLogger("llmbroker.broker")

# The tail the broker already reads for stats. A busy pool may push a once-a-day
# failure out of it; then there is no evidence and the entry stays.
_EVIDENCE_LIMIT = 1000
_NO_EVIDENCE: Mapping[str, Retirement] = MappingProxyType({})
_NO_TARGETS: Mapping[str, AliasTarget] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class SyncSource:
    """An arriving lineup and the curated preset name it came from."""

    label: str
    lineup: Lineup


@dataclass(frozen=True, slots=True)
class MergeOutcome:
    """What the merge decided, before any writer has seen it."""

    report: SyncReport
    lineup: Lineup
    refresh: AliasRefresh


# ── Naming the source ────────────────────────────────────────────────────────


async def load_sync_source(source: str, presets: PresetSource) -> SyncSource:
    """Fetch the named curated preset and parse it into the lineup to merge. The
    fetch is the library's one networked operation, and it runs off the event loop."""
    if not PRESET_NAME_RE.match(source):
        raise ValueError(
            f"unrecognized sync source {source!r} — a lineup arrives as a curated preset"
            " name (e.g. 'freetier') and nothing else",
        )
    text = await asyncio.to_thread(presets.text, source)
    return SyncSource(label=source, lineup=parse_lineup(tomllib.loads(text)))


# ── Death evidence, read from this installation's own journal ────────────────


def retirement_candidates(
    new_configs: list[LLMConfig],
    current_configs: list[LLMConfig],
    evidence: KeyEvidence,
) -> list[str]:
    """The entries the merge would otherwise keep, and only those. Empty on every
    ordinary sync, which is why the journal is normally not read; where a missing
    key proves nothing, every dropped entry is a candidate."""
    arriving = [c for c in new_configs if c.synced]
    names = {c.name for c in arriving}
    refs = {c.api_key_ref for c in arriving if c.api_key_ref}
    return [
        c.name
        for c in current_configs
        if c.synced
        and c.name not in names
        and c.api_key_ref not in refs
        and (c.api_key_ref in evidence.present or not evidence.visible)
    ]


async def dead_entries(
    names: Iterable[str],
    store: StoreProtocol | None,
    *,
    limit: int = _EVIDENCE_LIMIT,
) -> dict[str, Retirement]:
    """Those of ``names`` whose journal tail proves them unusable here, each with the
    evidence that condemned it. No key-hash condition: in a scoped installation the
    rule reads as "nobody could call it", which is the evidence wanted there."""
    wanted = set(names)
    if not wanted or not isinstance(store, QueryableStoreProtocol):
        return {}
    rows = await store.calls(limit=limit, kind="call")
    latest: dict[str, Call] = {}
    oldest: dict[str, Call] = {}
    alive: set[str] = set()
    for row in rows:
        if row.llm_name not in wanted:
            continue
        if row.status is CallStatus.OK:
            alive.add(row.llm_name)
        elif row.http_status is not None and is_permanent(row.http_status):
            # Newest first: the status is what the provider answers now, the
            # timestamp is how far back the run of failures reaches.
            latest.setdefault(row.llm_name, row)
            oldest[row.llm_name] = row
    return {
        name: Retirement(name=name, http_status=row.http_status, since=oldest[name].ts)
        for name, row in latest.items()
        if name not in alive
    }


# ── The merge ────────────────────────────────────────────────────────────────


def _check_model_identity(arriving: list[LLMConfig], current: dict[str, LLMConfig]) -> None:
    for cfg in arriving:
        stored = current.get(cfg.name)
        if stored is not None and stored.model != cfg.model:
            raise ValueError(
                f"sync: refusing to change model for {cfg.name!r}"
                f" (stored {stored.model!r} vs preset {cfg.model!r}) — a model bump"
                " must be a new entry name",
            )


def _check_name_clash(merged: list[LLMConfig]) -> None:
    """Refuse a name the merge itself would carry twice."""
    seen: dict[str, LLMConfig] = {}
    for cfg in merged:
        first = seen.get(cfg.name)
        if first is not None:
            hint = f"; {ALIAS_NAME_HINT}" if first.alias or cfg.alias else ""
            raise ValueError(
                f"the merged lineup would carry two entries named '{cfg.name}' —"
                " rename the entry this installation stated itself, the curated one"
                f" carries the name the preset formed{hint}",
            )
        seen[cfg.name] = cfg


def _removal_plan(
    dropped: list[LLMConfig],
    lineup_refs: set[str],
    evidence: KeyEvidence,
    dead: Mapping[str, Retirement],
) -> tuple[list[LLMConfig], list[LLMConfig], list[Retirement]]:
    """The provider is the unit: which dropped entries go, which stay, which retire.
    Depends only on the state of the world, which is what makes repeated syncs
    converge."""
    removed: list[LLMConfig] = []
    kept: list[LLMConfig] = []
    retired: list[Retirement] = []
    for entry in dropped:
        if entry.api_key_ref in lineup_refs:
            # The lineup's models for that provider replace it: same key, same
            # quota, so no key lookup is needed at all.
            removed.append(entry)
        elif evidence.visible and entry.api_key_ref not in evidence.present:
            removed.append(entry)
        elif entry.name in dead:
            removed.append(entry)
            retired.append(dead[entry.name])
        else:
            kept.append(entry)
    return removed, kept, retired


def _distinct_refs(entries: Iterable[LLMConfig]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(c.api_key_ref for c in entries if c.api_key_ref))


def _orphan_refs(
    removed: list[LLMConfig],
    merged: list[LLMConfig],
    present: frozenset[str],
) -> tuple[str, ...]:
    """Refs whose key exists here and which nothing in the merged lineup references
    any more. A ref with no key behind it is nothing to revoke, and would be noise
    on the commonest removal of all."""
    still_used = {c.api_key_ref for c in merged if c.api_key_ref}
    return tuple(ref for ref in _distinct_refs(removed) if ref not in still_used and ref in present)


def _pending_keys(
    merged: list[LLMConfig],
    keys: dict[str, KeyInfo],
    present: frozenset[str],
) -> tuple[PendingKey, ...]:
    holds: dict[str, list[str]] = {}
    for cfg in merged:
        if not cfg.api_key_ref or cfg.api_key_ref in present:
            continue
        holds.setdefault(cfg.api_key_ref, []).append(cfg.name)
    return tuple(
        PendingKey(
            api_key_ref=ref,
            help=keys[ref].help if ref in keys else "",
            entry_names=tuple(names),
        )
        for ref, names in holds.items()
    )


def merge_upstream(
    new: Lineup,
    current: Lineup,
    evidence: KeyEvidence,
    *,
    source: str,
    dead: Mapping[str, Retirement] = _NO_EVIDENCE,
) -> tuple[Lineup, SyncReport]:
    """Merge an arriving lineup into the current one. Pure — no I/O, no secrets.

    The partition is on ``synced``, not ``custom``: what a sync wrote is all a sync
    may replace or retire. Raises before the caller has written anything.
    """
    arriving = [c for c in new.configs if c.synced]
    stored_synced = [c for c in current.configs if c.synced]
    owned = [c for c in current.configs if not c.synced]
    current_by_name = {c.name: c for c in stored_synced}

    _check_model_identity(arriving, current_by_name)

    new_names = {c.name for c in arriving}
    lineup_refs = {c.api_key_ref for c in arriving if c.api_key_ref}
    dropped = [c for c in stored_synced if c.name not in new_names]
    arrived = [c for c in arriving if c.name not in current_by_name]
    removed, kept, retired = _removal_plan(dropped, lineup_refs, evidence, dead)

    merged = [*arriving, *kept, *owned]
    _check_name_clash(merged)

    keys = dict(new.keys)
    for cfg in (*kept, *owned):
        if cfg.api_key_ref and cfg.api_key_ref not in keys and cfg.api_key_ref in current.keys:
            keys[cfg.api_key_ref] = current.keys[cfg.api_key_ref]

    report = SyncReport(
        source=source,
        applied=True,
        added=tuple(c.name for c in arrived),
        updated=tuple(
            c.name for c in arriving if c.name in current_by_name and current_by_name[c.name] != c
        ),
        removed=tuple(c.name for c in removed),
        kept=tuple(c.name for c in kept),
        kept_refs=_distinct_refs(kept),
        retired=tuple(retired),
        orphan_refs=_orphan_refs(removed, merged, evidence.present),
        pending_keys=_pending_keys(merged, keys, evidence.present),
        active_before=sum(1 for c in current.configs if c.api_key_ref in evidence.present),
        active_after=sum(1 for c in merged if c.api_key_ref in evidence.present),
        keys_visible=evidence.visible,
        keys_scoped=evidence.scoped,
    )
    return Lineup(configs=merged, keys=keys), report


def check_not_emptying(
    merged: list[LLMConfig],
    current: list[LLMConfig],
    report: SyncReport,
) -> None:
    """The one structural guard: never apply an empty lineup over a working one.

    An empty target accepts anything — that is onboarding, not a loss.
    """
    if merged or not current:
        return
    raise SyncRefusedError(
        f"sync {report.source}: refusing to replace {len(current)} entries with an empty"
        " lineup — nothing was changed",
        report=replace(report, applied=False),
    )


# ── The one merge site, above both writers ──────────────────────────────────


async def merge_lineup(
    src: SyncSource,
    current: Lineup,
    *,
    probe: KeyProbe,
    store: StoreProtocol | None = None,
    alias_targets: Mapping[str, AliasTarget] = _NO_TARGETS,
) -> MergeOutcome:
    """Everything above the writer: re-point what follows an alias, weigh the keys,
    read the death evidence the decision needs, merge, and refuse an emptying.

    Whoever holds the lineup, the decision is made here; only the write differs.
    """
    configs, refresh = refresh_alias_configs(current.configs, alias_targets)
    keys = dict(current.keys)
    for ref, help_text in refresh.key_help.items():
        # The catalog fills a gap only: help already in the lineup wins.
        keys.setdefault(ref, KeyInfo(api_key_ref=ref, help=help_text, extra={}))
    refreshed = Lineup(configs=configs, keys=keys)

    evidence = await probe.evidence([c.api_key_ref for c in (*src.lineup.configs, *configs)])
    dead = await dead_entries(
        retirement_candidates(src.lineup.configs, configs, evidence),
        store,
    )
    merged, report = merge_upstream(
        src.lineup,
        refreshed,
        evidence,
        source=src.label,
        dead=dead,
    )
    check_not_emptying(merged.configs, configs, report)
    return MergeOutcome(report=report, lineup=merged, refresh=refresh)

"""The merge: an arriving model list becomes the model list this installation follows. One
decision site for a file and a database alike; see ``rules/model-list.md``."""

import asyncio
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, replace

from llmbroker.broker.presets import PRESET_NAME_RE, PresetSource
from llmbroker.exceptions import SyncRefusedError
from llmbroker.models import (
    KeyInfo,
    LLMConfig,
    ModelList,
    PendingKey,
    SyncReport,
)
from llmbroker.standalone.registry import parse_model_list


@dataclass(frozen=True, slots=True)
class SyncSource:
    """An arriving model list and the curated preset name it came from."""

    label: str
    model_list: ModelList


@dataclass(frozen=True, slots=True)
class MergeOutcome:
    """What the merge decided, before any writer has seen it."""

    report: SyncReport
    model_list: ModelList


# ── Naming the source ────────────────────────────────────────────────────────


async def load_sync_source(source: str, presets: PresetSource) -> SyncSource:
    """Fetch the named curated preset and parse it into the model list to merge. The
    fetch is the library's one networked operation, and it runs off the event loop."""
    if not PRESET_NAME_RE.match(source):
        raise ValueError(
            f"unrecognized sync source {source!r} — a model list arrives as a curated preset"
            " name (e.g. 'freetier') and nothing else",
        )
    text = await asyncio.to_thread(presets.text, source)
    return SyncSource(label=source, model_list=parse_model_list(tomllib.loads(text)))


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
    seen: set[str] = set()
    for cfg in merged:
        if cfg.name in seen:
            raise ValueError(
                f"the merged model list would carry two entries named '{cfg.name}' —"
                " rename the entry this installation stated itself, the curated one"
                " carries the name the preset formed",
            )
        seen.add(cfg.name)


def _distinct_refs(entries: Iterable[LLMConfig]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(c.api_key_ref for c in entries if c.api_key_ref))


def _orphan_refs(
    removed: list[LLMConfig],
    merged: list[LLMConfig],
    present: frozenset[str],
) -> tuple[str, ...]:
    """Refs whose key exists here and which nothing in the merged model list references
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
    new: ModelList,
    current: ModelList,
    present: frozenset[str],
    *,
    source: str,
) -> tuple[ModelList, SyncReport]:
    """Merge an arriving model list into the current one. Pure — no I/O, no secrets.

    ``present`` names the refs a key resolves for; the report describes them and no
    decision here reads them.
    """
    arriving = [c for c in new.configs if c.from_preset]
    stored_curated = [c for c in current.configs if c.from_preset]
    owned = [c for c in current.configs if not c.from_preset]
    current_by_name = {c.name: c for c in stored_curated}

    _check_model_identity(arriving, current_by_name)

    new_names = {c.name for c in arriving}
    removed = [c for c in stored_curated if c.name not in new_names]
    arrived = [c for c in arriving if c.name not in current_by_name]

    merged = [*arriving, *owned]
    _check_name_clash(merged)

    keys = dict(new.keys)
    for cfg in owned:
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
        orphan_refs=_orphan_refs(removed, merged, present),
        pending_keys=_pending_keys(merged, keys, present),
        active_before=sum(1 for c in current.configs if c.api_key_ref in present),
        active_after=sum(1 for c in merged if c.api_key_ref in present),
    )
    return ModelList(configs=merged, keys=keys), report


def check_not_emptying(
    merged: list[LLMConfig],
    current: list[LLMConfig],
    report: SyncReport,
) -> None:
    """The one structural guard: never apply an empty model list over a working one.

    An empty target accepts anything — that is onboarding, not a loss.
    """
    if merged or not current:
        return
    raise SyncRefusedError(
        f"sync {report.source}: refusing to replace {len(current)} entries with an empty"
        " model list — nothing was changed",
        report=replace(report, applied=False),
    )


# ── The one merge site, above both writers ──────────────────────────────────


def merge_model_list(
    src: SyncSource,
    current: ModelList,
    *,
    present: frozenset[str],
) -> MergeOutcome:
    """Everything above the writer: merge, and refuse an emptying.

    Whoever holds the model_list, the decision is made here; only the write differs.
    """
    merged, report = merge_upstream(src.model_list, current, present, source=src.label)
    check_not_emptying(merged.configs, current.configs, report)
    return MergeOutcome(report=report, model_list=merged)

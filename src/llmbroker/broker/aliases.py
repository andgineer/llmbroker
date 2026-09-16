"""The paid catalog and the declared models that follow one of its aliases.

The alias contract — what a re-resolution may rewrite, and what it may never move —
is in ``specs/reference/rules/direct-by-name.md``.
"""

import asyncio
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from types import MappingProxyType

from llmbroker.broker.curated import CuratedModel, models_from
from llmbroker.broker.presets import PAID_CATALOG, PresetSource
from llmbroker.models import DeclaredModels, LLMConfig


class AliasChange(Enum):
    """What a re-resolution found for one declared alias."""

    MODEL = "model"
    KEY_REF = "key_ref"
    DROPPED = "dropped"


@dataclass(frozen=True, slots=True)
class AliasFact:
    """One thing a re-resolution moved: the broker logs these and does not decide
    what they mean here."""

    change: AliasChange
    alias: str
    was: str = ""
    now: str = ""


_NO_TARGETS: Mapping[str, CuratedModel] = MappingProxyType({})


def catalog_alias_targets(catalog: dict) -> dict[str, CuratedModel]:
    """Map every catalog alias to the row it now recommends; a row whose provider has
    no endpoint or no key ref could not be called, so it recommends nothing. An alias
    names exactly one model, so a duplicate makes the whole file unusable and raises."""
    targets: dict[str, CuratedModel] = {}
    for row in models_from(catalog):
        provider = row.provider
        if row.alias is None or not (provider.id and provider.base_url and provider.api_key_ref):
            continue
        if row.alias in targets:
            raise ValueError(f"paid catalog is invalid — alias '{row.alias}' is used twice")
        targets[row.alias] = row
    return targets


async def resolve_declared(
    declared: Sequence[str | LLMConfig],
    presets: PresetSource,
    *,
    previous: DeclaredModels | None = None,
    fetch: bool = True,
) -> tuple[DeclaredModels, tuple[AliasFact, ...]]:
    """Turn what the caller declared with ``direct=`` into entries, with the catalog's
    key help — nothing stores a declared model, so this read is the only place that
    help is available. ``previous`` marks a re-resolution and is what the facts diff."""
    if not declared:
        return DeclaredModels(), ()
    targets, unreadable = await _catalog_targets(declared, presets, previous=previous, fetch=fetch)
    configs, unresolved, dropped = _each_declaration(declared, targets, unreadable, previous)
    resolved = DeclaredModels(
        configs=configs,
        key_help=_key_help(
            targets,
            configs,
            kept={fact.alias for fact in dropped},
            previous=previous,
        ),
        unresolved=MappingProxyType(unresolved),
    )
    return resolved, _moved(previous, resolved) + dropped


def _each_declaration(
    declared: Sequence[str | LLMConfig],
    targets: Mapping[str, CuratedModel],
    unreadable: str | None,
    previous: DeclaredModels | None,
) -> tuple[tuple[LLMConfig, ...], dict[str, str], tuple[AliasFact, ...]]:
    """Every declaration resolved on its own: the catalog's entry, the entry already in
    use where the catalog has dropped it, or the reason the call naming it will raise."""
    configs: list[LLMConfig] = []
    unresolved: dict[str, str] = {}
    dropped: list[AliasFact] = []
    for item in declared:
        if isinstance(item, LLMConfig):
            configs.append(item)
        elif (target := targets.get(item)) is not None:
            configs.append(replace(target.declare(), alias=item))
        elif (serving := _serving(previous, item)) is not None:
            configs.append(serving)
            dropped.append(AliasFact(change=AliasChange.DROPPED, alias=item, was=serving.model))
        else:
            unresolved[item] = _unresolved_message(item, targets, unreadable)
    return tuple(configs), unresolved, tuple(dropped)


def _key_help(
    targets: Mapping[str, CuratedModel],
    configs: tuple[LLMConfig, ...],
    *,
    kept: set[str],
    previous: DeclaredModels | None,
) -> dict[str, str]:
    """Where a key for these entries comes from. An entry kept because the catalog
    dropped its alias keeps the help it was resolved with: the read just made no longer
    names that provider, so it has nothing to say about where its key comes from."""
    wanted = {cfg.api_key_ref for cfg in configs}
    help_ = {
        t.provider.api_key_ref: t.provider.key_help
        for t in targets.values()
        if t.provider.key_help and t.provider.api_key_ref in wanted
    }
    was = previous.key_help if previous is not None else {}
    for cfg in configs:
        if cfg.alias in kept and cfg.api_key_ref not in help_ and cfg.api_key_ref in was:
            help_[cfg.api_key_ref] = was[cfg.api_key_ref]
    return help_


async def _catalog_targets(
    declared: Sequence[str | LLMConfig],
    presets: PresetSource,
    *,
    previous: DeclaredModels | None,
    fetch: bool,
) -> tuple[Mapping[str, CuratedModel], str | None]:
    """The catalog's alias targets, and — where it could not be read — why, which is
    ``None`` when it was read. A re-resolution raises that instead of reporting it: a
    read that failed says nothing about where an alias points."""
    if not any(isinstance(item, str) for item in declared):
        return _NO_TARGETS, None
    try:
        text = await asyncio.to_thread(
            presets.text,
            PAID_CATALOG,
            prefer_cache=True,
            floor=previous is None,
            fetch=fetch,
        )
        return catalog_alias_targets(tomllib.loads(text)), None
    except (ValueError, OSError) as exc:
        if previous is not None:
            raise
        return _NO_TARGETS, str(exc) or type(exc).__name__


def _serving(previous: DeclaredModels | None, alias: str) -> LLMConfig | None:
    """The entry this alias is already answering from, where there is one."""
    if previous is None:
        return None
    return next((cfg for cfg in previous.configs if cfg.alias == alias), None)


def _moved(previous: DeclaredModels | None, current: DeclaredModels) -> tuple[AliasFact, ...]:
    """What moved under the declared aliases since the last resolution. The first has
    nothing to compare against, and a version move is the only notice a deployment
    gets that ``direct("opus")`` now answers from a different model."""
    if previous is None:
        return ()
    was = {c.alias: c for c in previous.configs if c.alias is not None}
    facts: list[AliasFact] = []
    for cfg in current.configs:
        old = was.get(cfg.alias) if cfg.alias is not None else None
        if old is not None:
            facts.extend(_alias_facts(old, cfg))
    return tuple(facts)


def _unresolved_message(
    alias: str,
    targets: Mapping[str, CuratedModel],
    unreadable: str | None,
) -> str:
    """Why this handle has no entry, as the call naming it reports it. A typo is the
    expected failure and the fix is one word, so the message carries the words that
    would work."""
    if unreadable is not None:
        return f"direct= names {alias!r}, and the paid catalog could not be read: {unreadable}"
    have = ", ".join(sorted(targets)) or "none"
    return (
        f"direct= names {alias!r}, which the paid catalog does not carry"
        f" — available aliases: {have}"
    )


def _alias_facts(was: LLMConfig, now: LLMConfig) -> list[AliasFact]:
    alias = now.alias or ""
    facts = []
    if was.model != now.model:
        facts.append(
            AliasFact(change=AliasChange.MODEL, alias=alias, was=was.model, now=now.model),
        )
    if was.api_key_ref != now.api_key_ref:
        # The one change needing the user to act, and it can arrive without a model
        # change: a re-spelled ref wants an env var nobody set.
        facts.append(
            AliasFact(
                change=AliasChange.KEY_REF,
                alias=alias,
                was=was.api_key_ref,
                now=now.api_key_ref,
            ),
        )
    return facts

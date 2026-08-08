"""The paid catalog and the entries that follow one of its aliases.

The alias contract — what a refresh may rewrite, and what it may never move — is
in ``specs/reference/rules/direct-aliases.md``.
"""

import asyncio
import logging
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from types import MappingProxyType

from llmbroker.broker.presets import PAID_CATALOG, PresetSource
from llmbroker.exceptions import UnknownModelError
from llmbroker.models import DeclaredModels, LLMConfig

logger = logging.getLogger("llmbroker.broker")


@dataclass(frozen=True, slots=True)
class AliasTarget:
    """What the paid catalog currently recommends for one alias."""

    name: str
    model: str
    base_url: str
    api_key_ref: str
    key_help: str = ""


class AliasChange(Enum):
    """What a refresh found for one alias-following entry."""

    MODEL = "model"
    KEY_REF = "key_ref"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class AliasFact:
    """One thing a refresh changed, or could not: the CLI prints these, the broker
    logs them, and neither decides what they mean here."""

    change: AliasChange
    alias: str
    was: str = ""
    now: str = ""


@dataclass(frozen=True, slots=True)
class AliasRefresh:
    """What re-pointing the alias-following entries changed."""

    key_help: dict[str, str]
    facts: tuple[AliasFact, ...] = ()


_NO_TARGETS: Mapping[str, AliasTarget] = MappingProxyType({})
_NO_REFRESH = AliasRefresh(key_help={})


def catalog_alias_targets(catalog: dict) -> dict[str, AliasTarget]:
    """Map every catalog alias to the entry fields it now recommends.

    Raises when the catalog is invalid: an alias names exactly one model, so a
    duplicate makes the whole file unusable.
    """
    targets: dict[str, AliasTarget] = {}
    for prov in catalog.get("provider", []):
        if not isinstance(prov, dict) or not (
            prov.get("id") and prov.get("base_url") and prov.get("api_key_ref")
        ):
            continue
        for model in prov.get("models", []):
            if not isinstance(model, dict) or not (model.get("alias") and model.get("model")):
                continue
            alias = str(model["alias"])
            if alias in targets:
                raise ValueError(f"paid catalog is invalid — alias '{alias}' is used twice")
            targets[alias] = AliasTarget(
                name=f"{prov['id']}-{model['model']}",
                model=str(model["model"]),
                base_url=str(prov["base_url"]),
                api_key_ref=str(prov["api_key_ref"]),
                key_help=str(prov["key_help"]) if prov.get("key_help") else "",
            )
    return targets


async def alias_targets_for(
    aliases: Iterable[str | None],
    presets: PresetSource,
) -> dict[str, AliasTarget]:
    """What the catalog now recommends, read only when something follows an alias.

    A catalog nobody can reach yields no targets, and every alias entry is then left
    exactly as it is: a refresh that cannot see upstream has nothing to say about
    where an alias points.
    """
    if not any(aliases):
        return {}
    try:
        text = await asyncio.to_thread(presets.text, PAID_CATALOG, floor=False)
    except ValueError as exc:
        logger.warning("paid catalog unavailable (%s) — alias entries left untouched", exc)
        return {}
    return catalog_alias_targets(tomllib.loads(text))


async def resolve_declared(
    declared: Sequence[str | LLMConfig],
    presets: PresetSource,
    *,
    floor: bool = True,
) -> DeclaredModels:
    """Turn what the caller declared with ``direct=`` into entries.

    A config is taken verbatim and forced ``custom``; a string is a paid-catalog
    alias, resolved afresh so a followed model is always the current version. The
    catalog's key help comes back with them: nothing stores a declared model, so
    this read is the only place that help is ever available.

    ``floor=False`` is a re-resolution, which may not fall back to the wheel's copy.
    """
    if not declared:
        return DeclaredModels()
    targets: Mapping[str, AliasTarget] = _NO_TARGETS
    if any(isinstance(item, str) for item in declared):
        text = await asyncio.to_thread(
            presets.text,
            PAID_CATALOG,
            prefer_cache=True,
            floor=floor,
        )
        targets = catalog_alias_targets(tomllib.loads(text))
    configs = tuple(
        replace(item, custom=True)
        if isinstance(item, LLMConfig)
        else _entry_for_alias(item, targets)
        for item in declared
    )
    wanted = {cfg.api_key_ref for cfg in configs}
    return DeclaredModels(
        configs=configs,
        key_help={
            t.api_key_ref: t.key_help
            for t in targets.values()
            if t.key_help and t.api_key_ref in wanted
        },
    )


def _entry_for_alias(alias: str, targets: Mapping[str, AliasTarget]) -> LLMConfig:
    target = targets.get(alias)
    if target is None:
        # A typo is the expected failure and the fix is one word, so the message
        # has to carry the words that would work.
        have = ", ".join(sorted(targets)) or "none"
        raise UnknownModelError(
            f"direct= names {alias!r}, which the paid catalog does not carry"
            f" — available aliases: {have}",
        )
    return LLMConfig(
        name=target.name,
        base_url=target.base_url,
        model=target.model,
        api_key_ref=target.api_key_ref,
        custom=True,
        alias=alias,
    )


def _alias_facts(cfg: LLMConfig, target: AliasTarget) -> list[AliasFact]:
    alias = cfg.alias or ""
    facts = []
    if cfg.model != target.model:
        facts.append(
            AliasFact(change=AliasChange.MODEL, alias=alias, was=cfg.model, now=target.model),
        )
    if cfg.api_key_ref != target.api_key_ref:
        # The one change that needs the user to do something. It can arrive without
        # a model change at all — a catalog that re-spells a provider's ref refreshes
        # to a lineup that silently wants an env var nobody set.
        facts.append(
            AliasFact(
                change=AliasChange.KEY_REF,
                alias=alias,
                was=cfg.api_key_ref,
                now=target.api_key_ref,
            ),
        )
    return facts


def refresh_alias_configs(
    configs: list[LLMConfig],
    targets: Mapping[str, AliasTarget],
) -> tuple[list[LLMConfig], AliasRefresh]:
    """Re-point every alias-following entry at what the catalog now recommends."""
    if not targets:
        return configs, _NO_REFRESH
    key_help: dict[str, str] = {}
    facts: list[AliasFact] = []
    result: list[LLMConfig] = []
    for cfg in configs:
        target = targets.get(cfg.alias) if cfg.alias is not None else None
        if target is None:
            if cfg.alias is not None:
                facts.append(AliasFact(change=AliasChange.UNKNOWN, alias=cfg.alias))
            result.append(cfg)
            continue
        facts.extend(_alias_facts(cfg, target))
        result.append(
            replace(
                cfg,
                name=target.name,
                model=target.model,
                base_url=target.base_url,
                api_key_ref=target.api_key_ref,
            ),
        )
        if target.key_help:
            key_help[target.api_key_ref] = target.key_help
    return result, AliasRefresh(key_help=key_help, facts=tuple(facts))

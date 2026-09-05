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
from llmbroker.exceptions import UnknownModelError
from llmbroker.models import DeclaredModels, LLMConfig


class AliasChange(Enum):
    """What a re-resolution found for one declared alias."""

    MODEL = "model"
    KEY_REF = "key_ref"


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
    targets: Mapping[str, CuratedModel] = _NO_TARGETS
    if any(isinstance(item, str) for item in declared):
        text = await asyncio.to_thread(
            presets.text,
            PAID_CATALOG,
            prefer_cache=True,
            floor=previous is None,
            fetch=fetch,
        )
        targets = catalog_alias_targets(tomllib.loads(text))
    configs = tuple(
        item if isinstance(item, LLMConfig) else _entry_for_alias(item, targets)
        for item in declared
    )
    wanted = {cfg.api_key_ref for cfg in configs}
    resolved = DeclaredModels(
        configs=configs,
        key_help={
            t.provider.api_key_ref: t.provider.key_help
            for t in targets.values()
            if t.provider.key_help and t.provider.api_key_ref in wanted
        },
    )
    return resolved, _moved(previous, resolved)


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


def _entry_for_alias(alias: str, targets: Mapping[str, CuratedModel]) -> LLMConfig:
    target = targets.get(alias)
    if target is None:
        # A typo is the expected failure and the fix is one word, so the message
        # has to carry the words that would work.
        have = ", ".join(sorted(targets)) or "none"
        raise UnknownModelError(
            f"direct= names {alias!r}, which the paid catalog does not carry"
            f" — available aliases: {have}",
        )
    return replace(target.declare(), alias=alias)


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

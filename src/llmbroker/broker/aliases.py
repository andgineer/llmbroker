"""The paid catalog and the declared models that follow one of its aliases.

The alias contract — what a re-resolution may rewrite, and what it may never move —
is in ``specs/reference/rules/direct-aliases.md``.
"""

import asyncio
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from llmbroker.broker.presets import PAID_CATALOG, PresetSource
from llmbroker.exceptions import UnknownModelError
from llmbroker.models import DeclaredModels, LLMConfig


@dataclass(frozen=True, slots=True)
class AliasTarget:
    """What the paid catalog currently recommends for one alias."""

    name: str
    model: str
    base_url: str
    api_key_ref: str
    key_help: str = ""


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


_NO_TARGETS: Mapping[str, AliasTarget] = MappingProxyType({})


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
    targets: Mapping[str, AliasTarget] = _NO_TARGETS
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
            t.api_key_ref: t.key_help
            for t in targets.values()
            if t.key_help and t.api_key_ref in wanted
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
        alias=alias,
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

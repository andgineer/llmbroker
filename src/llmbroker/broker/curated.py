"""The curated files, readable as data by a program that has no broker yet.

What this read is not — a registry, a source of pool members, a network call — is in
``specs/reference/rules/direct-by-name.md``.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

from llmbroker.broker.presets import PAID_CATALOG, POOL_PRESET, PresetSource
from llmbroker.home import home_dir_for_read
from llmbroker.models import LLMConfig, ModelList
from llmbroker.standalone.registry import parse_model_list


@dataclass(frozen=True, slots=True)
class CuratedProvider:
    """One paid provider of the curated catalog: where its endpoint is and which key
    reference pays for it."""

    id: str
    base_url: str
    api_key_ref: str
    key_help: str = ""
    label: str = ""

    def declare(self, model: str) -> LLMConfig:
        """A declaration for any model id this provider serves, curated or not — the
        config ``direct=`` takes, pinned to that id and following no alias."""
        return LLMConfig(
            name=f"{self.id}-{model}",
            base_url=self.base_url,
            model=model,
            api_key_ref=self.api_key_ref,
        )


@dataclass(frozen=True, slots=True)
class CuratedModel:
    """One row of the curated paid catalog: a provider's model, with the alias that
    keeps it current where the catalog carries one."""

    provider: CuratedProvider
    model: str
    alias: str | None = None
    label: str = ""

    @property
    def name(self) -> str:
        return f"{self.provider.id}-{self.model}"

    def declare(self) -> LLMConfig:
        """A declaration pinned to this row's model id. The alias rides along as the
        catalog line it came from, and pins no less for being there — what follows an
        alias is a bare string passed to ``direct=``."""
        return LLMConfig(
            name=self.name,
            base_url=self.provider.base_url,
            model=self.model,
            api_key_ref=self.provider.api_key_ref,
            alias=self.alias,
        )


def _provider_from(entry: dict) -> CuratedProvider:
    return CuratedProvider(
        id=str(entry.get("id") or ""),
        base_url=str(entry.get("base_url") or ""),
        api_key_ref=str(entry.get("api_key_ref") or ""),
        key_help=str(entry.get("key_help") or ""),
        label=str(entry.get("label") or ""),
    )


def providers_from(catalog: dict) -> tuple[CuratedProvider, ...]:
    """Every provider the parsed paid catalog declares, in file order."""
    return tuple(
        _provider_from(entry) for entry in catalog.get("provider", []) if isinstance(entry, dict)
    )


def models_from(catalog: dict) -> tuple[CuratedModel, ...]:
    """Every model row of the parsed paid catalog, in file order. A row with no model
    id is not one; a provider missing a field yields its rows with that field empty."""
    models: list[CuratedModel] = []
    for entry in catalog.get("provider", []):
        if not isinstance(entry, dict):
            continue
        provider = _provider_from(entry)
        for row in entry.get("models", []):
            if not isinstance(row, dict) or not row.get("model"):
                continue
            models.append(
                CuratedModel(
                    provider=provider,
                    model=str(row["model"]),
                    alias=str(row["alias"]) if row.get("alias") else None,
                    label=str(row.get("label") or ""),
                ),
            )
    return tuple(models)


def _catalog_text(name: str, home: str | Path | None) -> str:
    """The curated text already on this machine, the wheel's copy under it. Never the
    network: an enumeration must not put a fetch behind an innocuous-looking read."""
    return PresetSource(home_dir_for_read(home)).text(name, prefer_cache=True, fetch=False)


def curated_providers(*, home: str | Path | None = None) -> tuple[CuratedProvider, ...]:
    """The paid providers llmbroker curates — base url and key ref for each, so a
    declaration for a model the catalog does not carry can be built."""
    return providers_from(tomllib.loads(_catalog_text(PAID_CATALOG, home)))


def curated_paid(*, home: str | Path | None = None) -> tuple[CuratedModel, ...]:
    """The paid models llmbroker curates, one row per tier."""
    return models_from(tomllib.loads(_catalog_text(PAID_CATALOG, home)))


def curated_pool(*, home: str | Path | None = None) -> ModelList:
    """The curated free model list, as a sync would read it."""
    return parse_model_list(tomllib.loads(_catalog_text(POOL_PRESET, home)))

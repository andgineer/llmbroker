"""The sync engine: fetch a curated lineup, merge it into the current one, write it.

Shared by the CLI's file target and the broker's own ``sync`` — the removal rule
lives here exactly once. See ``specs/reference/rules/sync-merge.md`` for the rule.
"""

import asyncio
import logging
import os
import re
import tempfile
import tomllib
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from http import HTTPStatus
from http.client import HTTPException
from importlib import resources
from pathlib import Path
from types import MappingProxyType

import tomli_w

from llmbroker.broker.catalog import resolve_key
from llmbroker.exceptions import SyncRefusedError, UnknownModelError
from llmbroker.models import (
    Call,
    CallStatus,
    DeclaredModels,
    KeyInfo,
    LLMConfig,
    PendingKey,
    Retirement,
    SyncReport,
)
from llmbroker.protocols.registry import KeyInfoProtocol, RegistryProtocol
from llmbroker.protocols.secrets import SecretsProtocol
from llmbroker.protocols.store import QueryableStoreProtocol, StoreProtocol
from llmbroker.standalone.registry import Registry, config_from_entry, key_info_from_entry

logger = logging.getLogger("llmbroker.broker")

PRESET_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_PRESET_URL = (
    "https://raw.githubusercontent.com/andgineer/llmbroker/main/src/llmbroker/presets/{name}.toml"
)
_FETCH_TIMEOUT = 10
_PERMANENT_FAILURES = frozenset(
    {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN, HTTPStatus.NOT_FOUND},
)
# The tail the broker already reads for stats. A busy pool may push a once-a-day
# failure out of it; then there is no evidence and the entry stays.
_EVIDENCE_LIMIT = 1000
_NO_EVIDENCE: Mapping[str, Retirement] = MappingProxyType({})
_KEPT_HEADER = (
    "# Kept from your previous lineup: the curated lineup no longer carries these\n"
    "# providers, and you still hold their keys. They keep routing until they stop\n"
    "# working. Generated block."
)


@dataclass(frozen=True, slots=True)
class AliasTarget:
    """What the paid catalog currently recommends for one alias."""

    name: str
    model: str
    base_url: str
    api_key_ref: str
    key_help: str = ""


@dataclass(frozen=True, slots=True)
class AliasRefresh:
    """What re-pointing the alias-following ``[[custom]]`` entries changed.

    ``notices`` and ``warnings`` are ready-to-show lines: the CLI prints them,
    the broker logs them.
    """

    key_help: dict[str, str]
    notices: tuple[str, ...]
    warnings: tuple[str, ...]


_NO_TARGETS: Mapping[str, AliasTarget] = MappingProxyType({})
_NO_REFRESH = AliasRefresh(key_help={}, notices=(), warnings=())


@dataclass(frozen=True, slots=True)
class SyncSource:
    """An arriving lineup, however it was named.

    ``text`` is the source's own file text when it has one, so a file target can
    keep its comments; ``preset`` marks the one form that came off the network.
    """

    label: str
    configs: list[LLMConfig]
    keys: dict[str, KeyInfo]
    text: str | None
    preset: bool


@dataclass(frozen=True, slots=True)
class FileSyncOutcome:
    """The result of syncing into a ``.toml`` file: the report, whether the target
    was rewritten, what was written, and the alias-refresh lines."""

    report: SyncReport
    changed: bool
    configs: tuple[LLMConfig, ...] = ()
    notices: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


# ── Fetching the curated lineup ──────────────────────────────────────────────


def fetch_preset_text(name: str) -> str:
    """Download ``presets/<name>.toml`` from the catalog.

    Every failure is a ``ValueError`` carrying an admin-readable message: nothing
    has been touched yet, so the sync simply does not happen.
    """
    if not PRESET_NAME_RE.match(name):
        raise ValueError(
            f"invalid preset name '{name}' (use letters, digits, hyphens, underscores)",
        )
    url = _PRESET_URL.format(name=name)
    # The read and the decode share the connect phase's try: a body that dies
    # mid-stream raises TimeoutError/IncompleteRead, and a caller that treats a
    # fetch failure as best-effort catches ValueError only.
    try:
        with urllib.request.urlopen(url, timeout=_FETCH_TIMEOUT) as resp:  # noqa: S310 - validated
            text = resp.read().decode()
    except urllib.error.HTTPError as exc:
        if exc.code == HTTPStatus.NOT_FOUND:
            raise ValueError(f"preset '{name}' not found in catalog") from exc
        raise ValueError(f"HTTP {exc.code} fetching {url}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(str(exc.reason)) from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"downloaded content for '{name}' is not valid UTF-8") from exc
    except (OSError, HTTPException) as exc:
        raise ValueError(f"failed reading {url}: {exc!r}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"downloaded content for '{name}' is not valid TOML") from exc
    _check_fetched_urls(name, data)
    return text


def _check_fetched_urls(name: str, data: dict) -> None:
    """A fetched lineup decides where this installation's API keys are sent, so the
    whole file is refused before any merge sees it. Not a defence against a
    compromised catalog — that one would serve ``https://`` too — it removes
    plaintext key transmission as an accident."""
    for section in ("llms", "custom", "provider"):
        for entry in data.get(section, []):
            url = entry.get("base_url") if isinstance(entry, dict) else None
            if url is not None and not str(url).startswith("https://"):
                raise ValueError(
                    f"preset '{name}' carries a non-https base_url ({url})"
                    " — refusing the whole file",
                )


def _cache_path(cache_dir: Path, name: str) -> Path:
    return cache_dir / "presets" / f"{name}.toml"


def _read_cached_preset(name: str, cache_dir: Path | None) -> str | None:
    if cache_dir is None:
        return None
    try:
        return _cache_path(cache_dir, name).read_text(encoding="utf-8")
    except OSError:
        return None


def bundled_preset_text(name: str) -> str | None:
    """The copy shipped in the wheel — the floor under the fallback chain, so a
    first run with no network and a cold cache still has a lineup to start from."""
    resource = resources.files("llmbroker").joinpath("presets", f"{name}.toml")
    try:
        return resource.read_text(encoding="utf-8")
    except (OSError, ModuleNotFoundError):
        return None


def _write_cached_preset(name: str, cache_dir: Path | None, text: str) -> None:
    if cache_dir is None:
        return
    target = _cache_path(cache_dir, name)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(target, text)
    except OSError as exc:
        logger.debug("preset %s: cannot cache into %s (%s)", name, target, exc)


def refresh_cached_preset(name: str, cache_dir: Path | None) -> None:
    """Pull a fresh copy into the cache. Raises when the fetch fails — the caller
    decides whether the copy already here is good enough."""
    _write_cached_preset(name, cache_dir, fetch_preset_text(name))


def cached_preset_text(name: str, cache_dir: Path | None = None, *, bundled: bool = True) -> str:
    """Fetch, keeping a copy beside it; on failure fall back to that copy, then to
    the one bundled in the wheel.

    Neither fallback is ever a source: a successful fetch overwrites the cache, a
    failed one — offline, or throttled by the CDN's per-IP limit — reads it, which
    is what keeps such an installation running on what it last saw.

    ``bundled=False`` drops the last step, for the caller that re-points entries
    someone already has: the copy shipped with this release is older than the
    repository by construction, so re-pointing from it would roll a stored model
    backwards. The wheel's copy is a floor for starting a lineup, never for
    moving one.
    """
    try:
        text = fetch_preset_text(name)
    except ValueError as exc:
        cached = _read_cached_preset(name, cache_dir)
        if cached is not None:
            logger.debug("preset %s: %s — falling back to the cached copy", name, exc)
            return cached
        bundled_text = bundled_preset_text(name) if bundled else None
        if bundled_text is None:
            raise
        # Louder than the cached fallback, which serves what this machine last saw:
        # the wheel's copy is frozen at the installed release, and a `preset` command
        # writing it into a file the user then keeps must not look like a fresh one.
        logger.warning(
            "preset %s: %s — falling back to the bundled copy, frozen at this"
            " llmbroker release and possibly older than the curated one",
            name,
            exc,
        )
        return bundled_text
    _write_cached_preset(name, cache_dir, text)
    return text


def paid_catalog_text(cache_dir: Path | None = None) -> str:
    """The paid catalog. Kept here so every network read goes through one seam."""
    return cached_preset_text("paid-catalog", cache_dir)


# ── Alias-following custom entries, refreshed from the paid catalog ──────────


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
    cache_dir: Path | None = None,
) -> dict[str, AliasTarget]:
    """What the catalog now recommends, read only when something follows an alias.

    A catalog nobody can reach yields no targets rather than the wheel's copy, and
    every alias entry is then left exactly as it is: a refresh that cannot see
    upstream has nothing to say about where an alias points.
    """
    if not any(aliases):
        return {}
    try:
        text = await asyncio.to_thread(
            cached_preset_text,
            "paid-catalog",
            cache_dir,
            bundled=False,
        )
    except ValueError as exc:
        logger.warning("paid catalog unavailable (%s) — alias entries left untouched", exc)
        return {}
    return catalog_alias_targets(tomllib.loads(text))


def local_paid_catalog_text(cache_dir: Path | None = None, *, bundled: bool = True) -> str:
    """The paid catalog without going to the network first: this machine's cached
    copy, then a fetch, and the wheel's copy as the floor under both.

    Declared models resolve through here on the refresh clock, so the common read
    is the local one. The bundled copy stays last: preferred over a fetch it would
    pin a declared alias to the version this release shipped with, for as long as
    the release is installed.

    ``bundled=False`` drops it entirely, for a re-resolution — the same rule the
    stored half already follows. Where nothing is writable there is no cache to
    read, so an offline re-resolution would otherwise land on the wheel's copy and
    move a working alias *backwards* to the version this release shipped with.
    """
    cached = _read_cached_preset("paid-catalog", cache_dir)
    if cached is not None:
        return cached
    return cached_preset_text("paid-catalog", cache_dir, bundled=bundled)


async def resolve_declared(
    declared: Sequence[str | LLMConfig],
    cache_dir: Path | None = None,
    *,
    bundled: bool = True,
) -> DeclaredModels:
    """Turn what the caller declared with ``direct=`` into entries.

    A config is taken verbatim and forced ``custom``; a string is a paid-catalog
    alias, resolved afresh so a followed model is always the current version. The
    catalog's key help comes back with them: nothing stores a declared model, so
    this read is the only place that help is ever available.
    """
    if not declared:
        return DeclaredModels()
    targets: Mapping[str, AliasTarget] = _NO_TARGETS
    if any(isinstance(item, str) for item in declared):
        text = await asyncio.to_thread(local_paid_catalog_text, cache_dir, bundled=bundled)
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


def _alias_notes(alias: str, was_model: object, was_ref: object, target: AliasTarget) -> list[str]:
    notes = []
    if was_model != target.model:
        notes.append(f"{alias}: {was_model} -> {target.model}")
    if was_ref != target.api_key_ref:
        # The one change that needs the user to do something. It can arrive
        # without a model change at all — a catalog that re-spells a provider's
        # ref refreshes to a file that silently wants an env var nobody set.
        notes.append(
            f"{alias}: api_key_ref {was_ref} -> {target.api_key_ref}"
            f" — set {target.api_key_ref} before the next call",
        )
    return notes


def _unknown_alias(alias: str) -> str:
    return f"alias '{alias}' is not in the paid catalog — entry left untouched"


def refresh_alias_entries(
    entries: list[dict],
    targets: Mapping[str, AliasTarget],
) -> AliasRefresh:
    """Re-point every alias entry at what the catalog now recommends, in place.

    The raw-dict half of the refresh: a file target is re-emitted from its own
    blocks, so whatever else the user wrote in them survives.
    """
    key_help: dict[str, str] = {}
    notices: list[str] = []
    warnings: list[str] = []
    for entry in entries:
        alias = entry.get("alias")
        if not alias:
            continue
        target = targets.get(str(alias))
        if target is None:
            warnings.append(_unknown_alias(str(alias)))
            continue
        notices.extend(
            _alias_notes(str(alias), entry.get("model"), entry.get("api_key_ref"), target),
        )
        entry["model"] = target.model
        entry["name"] = target.name
        entry["base_url"] = target.base_url
        entry["api_key_ref"] = target.api_key_ref
        if target.key_help:
            key_help[target.api_key_ref] = target.key_help
    return AliasRefresh(key_help=key_help, notices=tuple(notices), warnings=tuple(warnings))


def refresh_alias_configs(
    configs: list[LLMConfig],
    targets: Mapping[str, AliasTarget],
) -> tuple[list[LLMConfig], AliasRefresh]:
    """The same re-pointing for stored configs — a registry has no file text to keep."""
    if not targets:
        return configs, _NO_REFRESH
    key_help: dict[str, str] = {}
    notices: list[str] = []
    warnings: list[str] = []
    result: list[LLMConfig] = []
    for cfg in configs:
        target = targets.get(cfg.alias) if cfg.alias is not None else None
        if target is None:
            if cfg.alias is not None:
                warnings.append(_unknown_alias(cfg.alias))
            result.append(cfg)
            continue
        notices.extend(_alias_notes(cfg.alias or "", cfg.model, cfg.api_key_ref, target))
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
    return result, AliasRefresh(
        key_help=key_help,
        notices=tuple(notices),
        warnings=tuple(warnings),
    )


# ── Which refs we have a key for ─────────────────────────────────────────────


def declared_refs(have_keys: bool | Sequence[str]) -> set[str]:
    """The refs ``have_keys`` names outright."""
    if isinstance(have_keys, bool):
        return set()
    if isinstance(have_keys, str):
        return {have_keys}
    return set(have_keys)


def keys_are_visible(
    present: set[str],
    *,
    scope: str | None,
    have_keys: bool | Sequence[str],
) -> bool:
    """Absence of a key is evidence only where the probe could have found one.

    Per-user keys behind ``scope`` are one such place; a probe that resolved
    nothing at all is the other — the keys live in a store this merge site cannot
    reach, and "no key anywhere" is indistinguishable from "not my keys".
    """
    declared = have_keys is True or bool(declared_refs(have_keys))
    return bool(present) and (scope is None or declared)


async def present_refs(
    refs: Iterable[str],
    secrets: SecretsProtocol,
    *,
    scope: str | None,
    have_keys: bool | Sequence[str],
) -> set[str]:
    """The subset of ``refs`` a key exists for — resolvable, or declared via ``have_keys``.

    A declared ref counts as present only here, for weighing a removal; it never
    makes a model routable.
    """
    wanted = {ref for ref in refs if ref}
    if have_keys is True:
        return wanted
    # A bare string is a Sequence[str]: taken apart into characters it would
    # declare nothing and silently lose the caller's promise.
    declared = declared_refs(have_keys)
    present = wanted & declared
    for ref in wanted - present:
        if await resolve_key(secrets, ref, scope) is not None:
            present.add(ref)
    return present


# ── Death evidence, read from this installation's own journal ────────────────


def retirement_candidates(
    new_configs: list[LLMConfig],
    current_configs: list[LLMConfig],
    present: set[str],
    *,
    keys_visible: bool = True,
) -> list[str]:
    """The entries the merge would otherwise keep, and only those: managed, with
    name and provider both absent from the arriving lineup, that a missing key does
    not already remove.

    Empty on every ordinary sync, which is why the journal is normally not read.
    Where a missing key proves nothing — per-user keys, a probe that resolved
    nothing — every dropped entry is a candidate, since "nobody could call it" is
    the only evidence such an installation can ever produce.
    """
    new_managed = [c for c in new_configs if not c.custom]
    names = {c.name for c in new_managed}
    refs = {c.api_key_ref for c in new_managed if c.api_key_ref}
    return [
        c.name
        for c in current_configs
        if not c.custom
        and c.name not in names
        and c.api_key_ref not in refs
        and (c.api_key_ref in present or not keys_visible)
    ]


async def dead_entries(
    names: Iterable[str],
    store: StoreProtocol | None,
    *,
    limit: int = _EVIDENCE_LIMIT,
) -> dict[str, Retirement]:
    """Those of ``names`` whose journal tail proves them unusable here, each with
    the evidence that condemned it.

    Dead means at least one permanent client failure and no success at all in the
    window; a bad week — 429s, 5xx — proves nothing. No key-hash condition: in a
    scoped installation the rule reads as "nobody could call it", which is the
    evidence wanted there. Reads nothing when there is nothing to decide.
    """
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
        elif row.http_status in _PERMANENT_FAILURES:
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


def _check_model_identity(new_managed: list[LLMConfig], current: dict[str, LLMConfig]) -> None:
    for cfg in new_managed:
        stored = current.get(cfg.name)
        if stored is not None and stored.model != cfg.model:
            raise ValueError(
                f"sync: refusing to change model for {cfg.name!r}"
                f" (stored {stored.model!r} vs preset {cfg.model!r}) — a model bump"
                " must be a new entry name",
            )


def _check_name_clash(merged: list[LLMConfig]) -> None:
    seen: set[str] = set()
    for cfg in merged:
        if cfg.name in seen:
            raise ValueError(
                f"the merged file would carry two entries named '{cfg.name}' —"
                " rename the [[custom]] entry if it is pinned; an alias entry's name is"
                " machine-formed again on every refresh, so renaming it will not stick"
                " — drop its 'alias' to pin it instead",
            )
        seen.add(cfg.name)


def _removal_plan(
    dropped: list[LLMConfig],
    lineup_refs: set[str],
    present: set[str],
    dead: Mapping[str, Retirement],
    *,
    keys_visible: bool,
) -> tuple[list[LLMConfig], list[LLMConfig], list[Retirement]]:
    """The provider is the unit: which dropped entries go, which stay, which retire.

    Two entries on one ``api_key_ref`` are one quota and one failure domain, so the
    decision is about the ref, never about counting entries. Depends only on the
    state of the world, which is what makes repeated syncs converge.
    """
    removed: list[LLMConfig] = []
    kept: list[LLMConfig] = []
    retired: list[Retirement] = []
    for entry in dropped:
        if entry.api_key_ref in lineup_refs:
            # The lineup's models for that provider replace it: same key, same
            # quota, so no key lookup is needed at all.
            removed.append(entry)
        elif keys_visible and entry.api_key_ref not in present:
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
    present: set[str],
) -> tuple[str, ...]:
    """Refs whose key exists here and which nothing in the merged lineup references
    any more. ``[[custom]]`` counts as a reference, which is what keeps a paid direct
    model's key out of the "revoke it" advice; a ref with no key behind it is nothing
    to revoke and would be pure noise on the commonest removal of all."""
    still_used = {c.api_key_ref for c in merged if c.api_key_ref}
    return tuple(ref for ref in _distinct_refs(removed) if ref not in still_used and ref in present)


def _pending_keys(
    merged: list[LLMConfig],
    keys: dict[str, KeyInfo],
    present: set[str],
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


def merge_upstream(  # noqa: PLR0913
    new_configs: list[LLMConfig],
    new_keys: dict[str, KeyInfo],
    current_configs: list[LLMConfig],
    current_keys: dict[str, KeyInfo],
    present: set[str],
    *,
    source: str,
    dead: Mapping[str, Retirement] = _NO_EVIDENCE,
    keys_visible: bool = True,
    keys_scoped: bool = False,
) -> tuple[list[LLMConfig], dict[str, KeyInfo], SyncReport]:
    """Merge an arriving lineup into the current one. Pure — no I/O, no secrets.

    ``dead`` names entries this installation's journal proved unusable; the two
    ``keys_*`` flags say how much a missing key proves at this merge site.

    Raises ``ValueError`` on a model-identity change or a name carried twice;
    the caller has written nothing at that point.
    """
    new_managed = [c for c in new_configs if not c.custom]
    new_custom = [c for c in new_configs if c.custom]
    current_managed = [c for c in current_configs if not c.custom]
    current_custom = [c for c in current_configs if c.custom]
    current_by_name = {c.name: c for c in current_configs}

    _check_model_identity(new_managed, current_by_name)

    new_names = {c.name for c in new_managed}
    lineup_refs = {c.api_key_ref for c in new_managed if c.api_key_ref}
    dropped = [c for c in current_managed if c.name not in new_names]
    arrived = [c for c in new_managed if c.name not in current_by_name]
    removed, kept, retired = _removal_plan(
        dropped,
        lineup_refs,
        present,
        dead,
        keys_visible=keys_visible,
    )

    arriving_custom = {c.name for c in new_custom}
    custom = [*new_custom, *(c for c in current_custom if c.name not in arriving_custom)]
    merged = [*new_managed, *kept, *custom]
    _check_name_clash(merged)

    keys = dict(new_keys)
    for cfg in (*kept, *custom):
        if cfg.api_key_ref and cfg.api_key_ref not in keys and cfg.api_key_ref in current_keys:
            keys[cfg.api_key_ref] = current_keys[cfg.api_key_ref]

    report = SyncReport(
        source=source,
        applied=True,
        added=tuple(c.name for c in arrived),
        updated=tuple(
            c.name
            for c in new_managed
            if c.name in current_by_name and current_by_name[c.name] != c
        ),
        removed=tuple(c.name for c in removed),
        kept=tuple(c.name for c in kept),
        kept_refs=_distinct_refs(kept),
        retired=tuple(retired),
        orphan_refs=_orphan_refs(removed, merged, present),
        pending_keys=_pending_keys(merged, keys, present),
        active_before=sum(1 for c in current_configs if c.api_key_ref in present),
        active_after=sum(1 for c in merged if c.api_key_ref in present),
        keys_visible=keys_visible,
        keys_scoped=keys_scoped,
    )
    return merged, keys, report


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


# ── Writers ──────────────────────────────────────────────────────────────────


def entry_block(section: str, entry: dict) -> str:
    """One ``[[section]]`` block. The header is written here rather than left to
    ``tomli_w``, which renders a short array of tables as an inline top-level key —
    written after a trailing ``[keys.*]`` table that parses as a member of that table."""
    return f"[[{section}]]\n{tomli_w.dumps(entry).rstrip()}"


def _entry_dict(cfg: LLMConfig) -> dict:
    entry: dict = {}
    if cfg.alias is not None:
        entry["alias"] = cfg.alias
    entry["name"] = cfg.name
    entry["model"] = cfg.model
    entry["base_url"] = cfg.base_url
    entry["api_key_ref"] = cfg.api_key_ref
    if cfg.parallel is not None:
        entry["parallel"] = cfg.parallel
    if cfg.weight:
        entry["weight"] = cfg.weight
    return entry


def render_merged_toml(
    new_text: str,
    kept: list[LLMConfig],
    custom_entries: list[dict],
    keys_tail: dict,
) -> str:
    """The merged file: the arriving text verbatim (comments and all), then the kept
    entries, the ``[[custom]]`` blocks, and the ``[keys]`` those two still need."""
    parts = [new_text.rstrip("\n")]
    if kept:
        blocks = "\n\n".join(entry_block("llms", _entry_dict(cfg)) for cfg in kept)
        parts.append(f"{_KEPT_HEADER}\n{blocks}")
    parts.extend(entry_block("custom", entry) for entry in custom_entries)
    if keys_tail:
        parts.append(tomli_w.dumps({"keys": keys_tail}).rstrip("\n"))
    return "\n\n".join(parts) + "\n"


def write_atomic(target: Path, text: str) -> None:
    """Write through a sibling temp file and rename, so a crash mid-write cannot
    truncate the config the user already has."""
    # NamedTemporaryFile creates at 0600 and os.replace carries that onto the
    # target, so a runtime sync would lock everyone but its own user out of the
    # config it just rewrote.
    mode = target.stat().st_mode & 0o777 if target.exists() else None
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{target.name}.",
        delete=False,
    ) as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
        tmp = Path(fh.name)
    try:
        if mode is not None:
            tmp.chmod(mode)
        os.replace(tmp, target)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


# ── The file target, end to end ──────────────────────────────────────────────


def configs_from_data(data: dict) -> list[LLMConfig]:
    """The ``[[llms]]`` + ``[[custom]]`` entries of a parsed config, in file order."""
    configs: list[LLMConfig] = []
    for section, custom in (("llms", False), ("custom", True)):
        for entry in data.get(section, []):
            if not isinstance(entry, dict):
                continue
            cfg = config_from_entry(entry, custom=custom)
            if cfg is not None:
                configs.append(cfg)
    return configs


def key_infos_from_data(data: dict) -> dict[str, KeyInfo]:
    raw = data.get("keys", {})
    if not isinstance(raw, dict):
        return {}
    return {str(ref): key_info_from_entry(str(ref), val) for ref, val in raw.items()}


def resolve_sync_source(source: str) -> tuple[str, bool]:
    """Read a string source as an existing path, or as a curated preset name."""
    if Path(source).exists():
        return source, False
    if PRESET_NAME_RE.match(source) and not Path(source).suffix:
        return source, True
    raise ValueError(
        f"unrecognized sync source {source!r} — expected a curated preset name"
        " (e.g. 'freetier') or the path of an existing .toml/.json config file",
    )


async def load_sync_source(
    source: RegistryProtocol | str | Path,
    *,
    cache_dir: Path | None = None,
) -> SyncSource:
    """Load whatever was named into the lineup to merge. Only a preset name goes
    to the network, and it does so off the event loop."""
    if isinstance(source, str):
        name, is_preset = resolve_sync_source(source)
        if is_preset:
            text = await asyncio.to_thread(cached_preset_text, name, cache_dir)
            data = tomllib.loads(text)
            return SyncSource(
                label=name,
                configs=configs_from_data(data),
                keys=key_infos_from_data(data),
                text=text,
                preset=True,
            )
        source = Path(name)
    if isinstance(source, Path):
        if not source.exists():
            raise ValueError(f"no such file: {source}")
        source = Registry(source)
    keys = await source.key_info() if isinstance(source, KeyInfoProtocol) else {}
    path = source.path if isinstance(source, Registry) else None
    return SyncSource(
        label=str(path) if path is not None else type(source).__name__,
        configs=await source.load(),
        keys=keys,
        text=(
            path.read_text(encoding="utf-8")
            if path is not None and path.suffix.lower() == ".toml" and path.exists()
            else None
        ),
        preset=False,
    )


def _current_text(target: Path) -> str:
    """What the target holds now, ``""`` when it does not exist yet."""
    return target.read_text(encoding="utf-8") if target.exists() else ""


def _read_toml(target: Path) -> dict:
    if not target.exists():
        return {}
    try:
        return tomllib.loads(target.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ValueError(f"cannot read existing {target}: {exc}") from exc


def _keys_tail(
    entries: Iterable[LLMConfig],
    new_key_refs: set[str],
    existing_keys: dict,
    catalog_help: dict[str, str],
) -> dict:
    """The ``[keys]`` the re-emitted tail must carry: whatever the file already had
    for the refs of its kept and custom entries, plus catalog help for a new ref."""
    tail: dict = {}
    for cfg in entries:
        ref = cfg.api_key_ref
        if not ref or ref in new_key_refs or ref in tail:
            continue
        if ref in existing_keys:
            tail[ref] = existing_keys[ref]
        elif ref in catalog_help:
            tail[ref] = {"help": catalog_help[ref]}
    return tail


def _check_render_faithful(rendered: str, merged: list[LLMConfig]) -> None:
    """Belt and braces before the one write that can destroy a live config: what is
    about to replace it must parse, and carry exactly the merge's entries."""
    try:
        written = [c.name for c in configs_from_data(tomllib.loads(rendered))]
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(
            f"sync: the merged file would not parse ({exc}) — nothing written",
        ) from exc
    expected = [c.name for c in merged]
    if sorted(written) != sorted(expected):
        raise ValueError(
            f"sync: the merged file would carry {sorted(written)} instead of"
            f" {sorted(expected)} — nothing written",
        )


async def sync_file(  # noqa: PLR0913
    new_text: str,
    target: Path,
    *,
    source: str,
    secrets: SecretsProtocol,
    scope: str | None = None,
    have_keys: bool | Sequence[str] = False,
    dead: Mapping[str, Retirement] = _NO_EVIDENCE,
    alias_targets: Mapping[str, AliasTarget] = _NO_TARGETS,
) -> FileSyncOutcome:
    """Merge ``new_text`` into the ``.toml`` at ``target`` and write it atomically.

    ``alias_targets`` re-points the file's alias-following ``[[custom]]`` entries;
    who reads the catalog is the caller's business. Any error leaves ``target``
    untouched.
    """
    if target.suffix.lower() != ".toml":
        raise ValueError(f"sync target must be a .toml file, got {target}")
    data = _read_toml(target)
    custom_entries = [e for e in data.get("custom", []) if isinstance(e, dict)]
    refresh = refresh_alias_entries(custom_entries, alias_targets) if alias_targets else _NO_REFRESH

    new_data = tomllib.loads(new_text)
    new_configs = configs_from_data(new_data)
    current_configs = configs_from_data(data)
    present = await present_refs(
        [c.api_key_ref for c in (*new_configs, *current_configs)],
        secrets,
        scope=scope,
        have_keys=have_keys,
    )
    merged, _keys, report = merge_upstream(
        new_configs,
        key_infos_from_data(new_data),
        current_configs,
        key_infos_from_data(data),
        present,
        source=source,
        dead=dead,
        keys_visible=keys_are_visible(present, scope=scope, have_keys=have_keys),
        keys_scoped=scope is not None,
    )
    check_not_emptying(merged, current_configs, report)

    kept_names = set(report.kept)
    kept = [c for c in merged if c.name in kept_names]
    tail = _keys_tail(
        [*kept, *(c for c in merged if c.custom)],
        set(new_data.get("keys", {})),
        data.get("keys", {}),
        refresh.key_help,
    )
    rendered = render_merged_toml(new_text, kept, custom_entries, tail)
    # Compared on the exact bytes that would be written, so an upstream change
    # carrying only a comment still counts: the file's git history is the record.
    changed = rendered != _current_text(target)
    if changed:
        _check_render_faithful(rendered, merged)
        write_atomic(target, rendered)
    return FileSyncOutcome(
        report=report,
        changed=changed,
        configs=tuple(merged),
        notices=refresh.notices,
        warnings=refresh.warnings,
    )

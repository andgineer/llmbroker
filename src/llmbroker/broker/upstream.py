"""The sync engine: fetch a curated lineup, merge it into the current one, write it.

Shared by the CLI's file target and the broker's own ``sync`` — the removal rule
lives here exactly once. See ``specs/reference/architecture.md`` for the rule.
"""

import asyncio
import logging
import os
import re
import tempfile
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from http import HTTPStatus
from pathlib import Path

import tomli_w

from llmbroker.broker.catalog import resolve_key
from llmbroker.exceptions import SyncRefusedError
from llmbroker.models import KeyInfo, LLMConfig, PendingKey, SyncReport
from llmbroker.protocols.registry import KeyInfoProtocol, RegistryProtocol
from llmbroker.protocols.secrets import SecretsProtocol
from llmbroker.standalone.registry import Registry, config_from_entry, key_info_from_entry

logger = logging.getLogger("llmbroker.broker")

PRESET_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_PRESET_URL = "https://raw.githubusercontent.com/andgineer/llmbroker/main/presets/{name}.toml"
_FETCH_TIMEOUT = 10
_KEPT_HEADER = (
    "# Kept from your previous lineup: upstream dropped these and no replacement you\n"
    "# can call arrived. The next sync removes them once one does. Generated block."
)


@dataclass(frozen=True, slots=True)
class AliasRefresh:
    """What re-pointing the alias-following ``[[custom]]`` entries changed.

    ``notices`` and ``warnings`` are ready-to-show lines: the CLI prints them,
    the broker logs them.
    """

    key_help: dict[str, str]
    notices: tuple[str, ...]
    warnings: tuple[str, ...]


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
    """The result of syncing into a ``.toml`` file: the report plus the alias-refresh lines."""

    report: SyncReport
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
    try:
        with urllib.request.urlopen(url, timeout=_FETCH_TIMEOUT) as resp:  # noqa: S310 - validated
            content = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == HTTPStatus.NOT_FOUND:
            raise ValueError(f"preset '{name}' not found in catalog") from exc
        raise ValueError(f"HTTP {exc.code} fetching {url}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(str(exc.reason)) from exc
    try:
        text = content.decode()
    except UnicodeDecodeError as exc:
        raise ValueError(f"downloaded content for '{name}' is not valid UTF-8") from exc
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"downloaded content for '{name}' is not valid TOML") from exc
    return text


def paid_catalog_text() -> str:
    """The paid catalog. Kept here so every network read goes through one seam."""
    return fetch_preset_text("paid-catalog")


# ── Alias-following custom entries, refreshed from the paid catalog ──────────


def catalog_alias_index(catalog: dict) -> dict[str, tuple[dict, dict]]:
    """Map every catalog alias to its ``(provider, model)`` pair.

    Raises when the catalog is invalid: an alias names exactly one model, so a
    duplicate makes the whole file unusable.
    """
    index: dict[str, tuple[dict, dict]] = {}
    for prov in catalog.get("provider", []):
        if not isinstance(prov, dict) or not (
            prov.get("id") and prov.get("base_url") and prov.get("api_key_ref")
        ):
            continue
        for model in prov.get("models", []):
            if not isinstance(model, dict) or not (model.get("alias") and model.get("model")):
                continue
            alias = str(model["alias"])
            if alias in index:
                raise ValueError(f"paid catalog is invalid — alias '{alias}' is used twice")
            index[alias] = (prov, model)
    return index


def refresh_alias_entries(
    entries: list[dict],
    index: dict[str, tuple[dict, dict]],
) -> AliasRefresh:
    """Re-point every alias entry at what the catalog now recommends, in place."""
    key_help: dict[str, str] = {}
    notices: list[str] = []
    warnings: list[str] = []
    for entry in entries:
        alias = entry.get("alias")
        if not alias:
            continue
        found = index.get(str(alias))
        if found is None:
            warnings.append(f"alias '{alias}' is not in the paid catalog — entry left untouched")
            continue
        prov, model = found
        was_model = entry.get("model")
        was_ref = entry.get("api_key_ref")
        entry["model"] = str(model["model"])
        entry["name"] = f"{prov['id']}-{model['model']}"
        entry["base_url"] = str(prov["base_url"])
        entry["api_key_ref"] = str(prov["api_key_ref"])
        if prov.get("key_help"):
            key_help[str(prov["api_key_ref"])] = str(prov["key_help"])
        if was_model != entry["model"]:
            notices.append(f"{alias}: {was_model} -> {entry['model']}")
        if was_ref != entry["api_key_ref"]:
            # The one change that needs the user to do something. It can arrive
            # without a model change at all — a catalog that re-spells a provider's
            # ref refreshes to a file that silently wants an env var nobody set.
            notices.append(
                f"{alias}: api_key_ref {was_ref} -> {entry['api_key_ref']}"
                f" — set {entry['api_key_ref']} before the next call",
            )
    return AliasRefresh(key_help=key_help, notices=tuple(notices), warnings=tuple(warnings))


# ── Which refs we have a key for ─────────────────────────────────────────────


async def present_refs(
    refs: Iterable[str],
    secrets: SecretsProtocol,
    *,
    scope: str | None,
    have_keys: bool | Sequence[str],
) -> set[str]:
    """The subset of ``refs`` a key exists for — resolvable, or declared via ``have_keys``.

    A declared ref counts as present only here, for paying removals; it never
    makes a model routable.
    """
    wanted = {ref for ref in refs if ref}
    if have_keys is True:
        return wanted
    present = set() if have_keys is False else wanted & set(have_keys)
    for ref in wanted - present:
        if await resolve_key(secrets, ref, scope) is not None:
            present.add(ref)
    return present


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
    arrived: list[LLMConfig],
    present: set[str],
) -> tuple[list[LLMConfig], list[LLMConfig]]:
    """Pairs, then budget: which dropped entries are paid for, and which stay.

    An arrival carrying a dropped entry's ref *is* its replacement and needs no
    key lookup at all; every other arrival pays for one removal only if we have
    its key. Entries whose own key is absent are spent first — they are inactive
    already, so the callable count stays as high as possible.
    """
    remaining = list(dropped)
    removed: list[LLMConfig] = []
    paid: set[str] = set()
    for arrival in arrived:
        if not arrival.api_key_ref:
            continue
        match = next((d for d in remaining if d.api_key_ref == arrival.api_key_ref), None)
        if match is not None:
            removed.append(match)
            remaining.remove(match)
            paid.add(arrival.name)
    budget = sum(1 for a in arrived if a.name not in paid and a.api_key_ref in present)
    order = [d for d in remaining if d.api_key_ref not in present]
    order += [d for d in remaining if d.api_key_ref in present]
    removed.extend(order[:budget])
    return removed, order[budget:]


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
) -> tuple[list[LLMConfig], dict[str, KeyInfo], SyncReport]:
    """Merge an arriving lineup into the current one. Pure — no I/O, no secrets.

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
    dropped = [c for c in current_managed if c.name not in new_names]
    arrived = [c for c in new_managed if c.name not in current_by_name]
    removed, kept = _removal_plan(dropped, arrived, present)

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
        pending_keys=_pending_keys(merged, keys, present),
        active_before=sum(1 for c in current_configs if c.api_key_ref in present),
        active_after=sum(1 for c in merged if c.api_key_ref in present),
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
    if not cfg.pooled:
        entry["pool"] = False
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


async def load_sync_source(source: RegistryProtocol | str | Path) -> SyncSource:
    """Load whatever was named into the lineup to merge. Only a preset name goes
    to the network, and it does so off the event loop."""
    if isinstance(source, str):
        name, is_preset = resolve_sync_source(source)
        if is_preset:
            text = await asyncio.to_thread(fetch_preset_text, name)
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


def render_lineup(configs: list[LLMConfig], keys: dict[str, KeyInfo]) -> str:
    """A lineup as TOML text — the stand-in for a source that has no file of its own."""
    parts = [
        entry_block("custom" if cfg.custom else "llms", _entry_dict(cfg))
        for cfg in sorted(configs, key=lambda c: c.custom)
    ]
    table = {ref: {"help": info.help, **info.extra} for ref, info in keys.items()}
    if table:
        parts.append(tomli_w.dumps({"keys": table}).rstrip("\n"))
    return "\n\n".join(parts) + "\n" if parts else ""


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


async def sync_file(  # noqa: PLR0913
    new_text: str,
    target: Path,
    *,
    source: str,
    secrets: SecretsProtocol,
    scope: str | None = None,
    have_keys: bool | Sequence[str] = False,
    fetch_catalog: Callable[[], str] | None = None,
) -> FileSyncOutcome:
    """Merge ``new_text`` into the ``.toml`` at ``target`` and write it atomically.

    ``fetch_catalog`` enables the alias-following refresh of ``[[custom]]`` entries
    against the paid catalog, and is called only when the file has such an entry.
    Any error leaves ``target`` untouched.
    """
    if target.suffix.lower() != ".toml":
        raise ValueError(f"sync target must be a .toml file, got {target}")
    data = _read_toml(target)
    custom_entries = [e for e in data.get("custom", []) if isinstance(e, dict)]

    refresh = AliasRefresh(key_help={}, notices=(), warnings=())
    if fetch_catalog is not None and any(e.get("alias") for e in custom_entries):
        catalog = tomllib.loads(await asyncio.to_thread(fetch_catalog))
        refresh = refresh_alias_entries(custom_entries, catalog_alias_index(catalog))

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
    write_atomic(target, render_merged_toml(new_text, kept, custom_entries, tail))
    return FileSyncOutcome(report=report, notices=refresh.notices, warnings=refresh.warnings)

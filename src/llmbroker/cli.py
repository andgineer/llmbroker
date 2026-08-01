"""python -m llmbroker <command>.

Subcommands: env (emit .env skeleton), preset (download curated preset TOML,
or --merge it into a file, refreshing alias-following custom entries from the
paid catalog), add-model (append a paid model from the catalog), sync (mirror a
preset TOML into a DB registry — DB-init workflow).
"""

import argparse
import asyncio
import os
import re
import sys
import tempfile
import tomllib
import urllib.error
import urllib.request
from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path

import tomli_w

from llmbroker.broker.broker import AsyncBroker
from llmbroker.models import KeyInfo, LLMConfig
from llmbroker.standalone.registry import Registry, key_info_from_entry

_PRESET_URL = "https://raw.githubusercontent.com/andgineer/llmbroker/main/presets/{name}.toml"
_PRESET_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


async def _env_data(reg: Registry) -> tuple[list[LLMConfig], dict[str, KeyInfo]]:
    return await reg.load(), await reg.key_info()


def _env_source_data(source: str) -> tuple[list[LLMConfig], dict[str, KeyInfo]] | None:
    """Load the ``env`` argument as an existing config file, or as a preset name
    fetched from the catalog — so onboarding needs no local file at all."""
    path = Path(source)
    if path.exists():
        return asyncio.run(_env_data(Registry(path)))
    if not _PRESET_NAME_RE.match(source):
        print(
            f"error: no such file: {path} (and {source!r} is not a valid preset name)",
            file=sys.stderr,
        )
        return None
    text = _fetch_preset_file(source)
    if text is None:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / f"{source}.toml"
        staged.write_text(text, encoding="utf-8")
        return asyncio.run(_env_data(Registry(staged)))


def _cmd_env(args: argparse.Namespace) -> int:
    # llms in file order; infos maps ref -> KeyInfo only for refs with a [keys] entry.
    data = _env_source_data(args.config)
    if data is None:
        return 1
    configs, infos = data

    refs: list[str] = []
    for cfg in configs:
        if cfg.api_key_ref and cfg.api_key_ref not in refs:
            refs.append(cfg.api_key_ref)

    lines: list[str] = []
    for ref in refs:
        info = infos.get(ref) or key_info_from_entry(ref, None)
        if info.help.strip():
            for i, line in enumerate(info.help.splitlines()):
                lines.append(f"# {ref} — {line}" if i == 0 else f"# {line}")
        if ref in os.environ:
            lines.append(f"# {ref} already set")
        else:
            lines.append(f"{ref}=")
    print("\n".join(lines))
    return 0


def _fetch_preset_file(name: str) -> str | None:
    """Download ``presets/<name>.toml`` from the repo; ``None`` (with a message on
    stderr) on any failure. Shared by ``preset`` and ``add-model``."""
    if not _PRESET_NAME_RE.match(name):
        print(
            f"error: invalid preset name '{name}' (use letters, digits, hyphens, underscores)",
            file=sys.stderr,
        )
        return None
    url = _PRESET_URL.format(name=name)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 - name validated above
            content = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == HTTPStatus.NOT_FOUND:
            print(f"error: preset '{name}' not found in catalog", file=sys.stderr)
        else:
            print(f"error: HTTP {exc.code} fetching {url}", file=sys.stderr)
        return None
    except urllib.error.URLError as exc:
        print(f"error: {exc.reason}", file=sys.stderr)
        return None
    try:
        text = content.decode()
    except UnicodeDecodeError:
        print(f"error: downloaded content for '{name}' is not valid UTF-8", file=sys.stderr)
        return None
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        print(f"error: downloaded content for '{name}' is not valid TOML", file=sys.stderr)
        return None
    return text


def _cmd_preset(args: argparse.Namespace) -> int:
    text = _fetch_preset_file(args.name)
    if text is None:
        return 1
    if args.merge is not None:
        return _merge_preset(text, args.name, Path(args.merge))
    sys.stdout.write(text)
    return 0


def _catalog_alias_index(catalog: dict) -> dict[str, tuple[dict, dict]] | None:
    """Map every catalog alias to its ``(provider, model)`` pair.

    ``None`` (with a message) when the catalog is invalid: an alias names exactly
    one model, so a duplicate makes the whole file unusable.
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
                print(
                    f"error: paid catalog is invalid — alias '{alias}' is used twice",
                    file=sys.stderr,
                )
                return None
            index[alias] = (prov, model)
    return index


def _refresh_alias_entries(
    entries: list[dict],
    index: dict[str, tuple[dict, dict]],
) -> dict[str, str]:
    """Re-point every alias entry at what the catalog now recommends, in place.

    Returns the ``api_key_ref -> key_help`` pairs the refreshed entries need, so a
    provider switch brings its onboarding help along.
    """
    key_help: dict[str, str] = {}
    for entry in entries:
        alias = entry.get("alias")
        if not alias:
            continue
        found = index.get(str(alias))
        if found is None:
            print(
                f"warning: alias '{alias}' is not in the paid catalog — entry left untouched",
                file=sys.stderr,
            )
            continue
        prov, model = found
        was = entry.get("model")
        entry["model"] = str(model["model"])
        entry["name"] = f"{prov['id']}-{model['model']}"
        entry["base_url"] = str(prov["base_url"])
        entry["api_key_ref"] = str(prov["api_key_ref"])
        if prov.get("key_help"):
            key_help[str(prov["api_key_ref"])] = str(prov["key_help"])
        if was != entry["model"]:
            print(f"{alias}: {was} -> {entry['model']}")
    return key_help


def _merged_name_clash(preset_text: str, custom_entries: list[dict]) -> str | None:
    """The error for a name the merged file would carry twice, or ``None``.

    A refreshed alias entry takes a machine-formed name in the preset's own
    convention, so a catalog move can land it on a preset pool entry. Shipping
    that file would lose one of the two at the next sync, which keys on the name.
    """
    seen: set[str] = set()
    preset_llms = tomllib.loads(preset_text).get("llms", [])
    for entry in [*preset_llms, *custom_entries]:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        name = str(entry["name"])
        if name in seen:
            return (
                f"error: the merged file would carry two entries named '{name}' —"
                " rename the [[custom]] entry, or drop its 'alias' so refreshes stop"
                " renaming it"
            )
        seen.add(name)
    return None


def _custom_key_tail(
    custom_entries: list[dict],
    existing_keys: dict,
    preset_key_refs: set,
    new_key_help: dict[str, str],
) -> dict:
    """The ``[keys]`` the re-emitted ``[[custom]]`` tail must carry: whatever the
    file already had for its own refs, plus catalog help for a ref new to it."""
    refs: list[str] = []
    for entry in custom_entries:
        ref = entry.get("api_key_ref")
        if ref and str(ref) not in refs:
            refs.append(str(ref))
    keys: dict = {}
    for ref in refs:
        if ref in preset_key_refs:
            continue
        if ref in existing_keys:
            keys[ref] = existing_keys[ref]
        elif ref in new_key_help:
            keys[ref] = {"help": new_key_help[ref]}
    return keys


def _merge_preset(preset_text: str, name: str, target: Path) -> int:
    """Refresh the managed ``[[llms]]`` + ``[keys]`` in ``target`` from a fresh preset,
    keeping the user's ``[[custom]]`` models and their keys. A custom entry carrying an
    ``alias`` also follows the paid catalog, so its provider fields are rewritten too.
    Managed comments come from the preset verbatim; the re-emitted ``[[custom]]`` tail
    loses inline comments. Atomic: any error leaves ``target`` untouched."""
    if target.suffix.lower() != ".toml":
        print(f"error: --merge target must be a .toml file, got {target}", file=sys.stderr)
        return 1
    try:
        existing = tomllib.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    except (tomllib.TOMLDecodeError, OSError) as exc:
        print(f"error: cannot read existing {target}: {exc}", file=sys.stderr)
        return 1

    custom_entries = [e for e in existing.get("custom", []) if isinstance(e, dict)]
    new_key_help: dict[str, str] = {}
    if any(e.get("alias") for e in custom_entries):
        catalog_text = _fetch_preset_file("paid-catalog")
        if catalog_text is None:
            return 1
        index = _catalog_alias_index(tomllib.loads(catalog_text))
        if index is None:
            return 1
        new_key_help = _refresh_alias_entries(custom_entries, index)

    clash = _merged_name_clash(preset_text, custom_entries)
    if clash is not None:
        print(clash, file=sys.stderr)
        return 1

    custom_keys = _custom_key_tail(
        custom_entries,
        existing.get("keys", {}),
        set(tomllib.loads(preset_text).get("keys", {})),
        new_key_help,
    )

    tail: dict = {}
    if custom_entries:
        tail["custom"] = custom_entries
    if custom_keys:
        tail["keys"] = custom_keys

    parts = [preset_text.rstrip("\n")]
    if tail:
        parts.append(tomli_w.dumps(tail).rstrip("\n"))
    target.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    print(f"merged preset '{name}' into {target} (kept {len(custom_entries)} [[custom]] entries)")
    return 0


def _provider_menu_label(prov: dict) -> str:
    return str(prov.get("label") or prov.get("id") or "")


def _model_menu_label(model: dict) -> str:
    """``alias — label (current model)`` — the alias leads, since it is what the
    application will pass to ``direct()`` and the only part that never changes."""
    label = model.get("label") or model.get("model")
    alias = model.get("alias")
    return f"{alias} — {label} ({model.get('model')})" if alias else str(label)


def _prompt_choice(
    label: str,
    items: list[dict],
    display: "Callable[[dict], str]",
) -> dict | None:
    """Print a numbered menu of ``items`` and read a pick."""
    for i, item in enumerate(items, 1):
        print(f"  {i}. {display(item)}")
    raw = input(f"{label} [1-{len(items)}]: ").strip()
    try:
        idx = int(raw)
    except ValueError:
        print(f"error: not a number: {raw!r}", file=sys.stderr)
        return None
    if not 1 <= idx <= len(items):
        print(f"error: choice out of range: {idx}", file=sys.stderr)
        return None
    return items[idx - 1]


def _select_from_flags(
    providers: list[dict],
    provider_id: str | None,
    model_id: str | None,
) -> tuple[dict, dict] | None:
    if provider_id is None or model_id is None:
        print("error: --provider and --model must be given together", file=sys.stderr)
        return None
    prov = next((p for p in providers if p.get("id") == provider_id), None)
    if prov is None:
        have = ", ".join(str(p.get("id")) for p in providers)
        print(f"error: unknown provider '{provider_id}' (have: {have})", file=sys.stderr)
        return None
    models = [m for m in prov.get("models", []) if isinstance(m, dict) and m.get("model")]
    model = next((m for m in models if str(m["model"]) == model_id), None)
    if model is None:
        have = ", ".join(str(m["model"]) for m in models)
        print(f"error: unknown model '{model_id}' (have: {have})", file=sys.stderr)
        return None
    return prov, model


def _select_interactive(providers: list[dict]) -> tuple[dict, dict] | None:
    prov = _prompt_choice("Pick a provider", providers, _provider_menu_label)
    if prov is None:
        return None
    models = [m for m in prov.get("models", []) if isinstance(m, dict) and m.get("model")]
    if not models:
        print(f"error: provider '{prov.get('id')}' has no usable models", file=sys.stderr)
        return None
    model = _prompt_choice("Pick a model", models, _model_menu_label)
    if model is None:
        return None
    return prov, model


def _select_provider_model(
    providers: list[dict],
    provider_id: str | None,
    model_id: str | None,
) -> tuple[dict, dict] | None:
    """Resolve (provider, catalog model) from flags, or interactively. ``None`` on error."""
    if provider_id is not None or model_id is not None:
        return _select_from_flags(providers, provider_id, model_id)
    return _select_interactive(providers)


def _cmd_add_model(args: argparse.Namespace) -> int:  # noqa: PLR0911
    target = Path(args.into)
    if target.suffix.lower() != ".toml":
        print(f"error: --into target must be a .toml file, got {target}", file=sys.stderr)
        return 1
    if args.name and not args.pin:
        print(
            "error: --name is only valid with --pin — an alias entry's name is machine-formed"
            " from the provider and model ids, and rewritten by every catalog refresh",
            file=sys.stderr,
        )
        return 1
    text = _fetch_preset_file("paid-catalog")
    if text is None:
        return 1
    providers = [p for p in tomllib.loads(text).get("provider", []) if isinstance(p, dict)]
    if not providers:
        print("error: paid catalog has no providers", file=sys.stderr)
        return 1

    interactive = args.provider is None and args.model is None
    selection = _select_provider_model(providers, args.provider, args.model)
    if selection is None:
        return 1
    prov, model = selection
    if not (prov.get("base_url") and prov.get("api_key_ref")):
        print(
            f"error: catalog entry for '{prov.get('id')}' is incomplete"
            " (needs base_url and api_key_ref)",
            file=sys.stderr,
        )
        return 1
    model_id = str(model["model"])
    if not args.pin and not model.get("alias"):
        print(
            f"error: catalog model '{model_id}' carries no alias — pass --pin to add it"
            " as a version-pinned entry instead",
            file=sys.stderr,
        )
        return 1
    alias = None if args.pin else str(model["alias"])

    if args.pin:
        default_name = (args.name or str(prov["id"])).strip()
        name = _prompt_name(default_name) if interactive else default_name
    else:
        name = f"{prov['id']}-{model_id}"
    pooled = (
        _prompt_yes_no("Add to the pool (failover)?", default=bool(args.pool))
        if interactive
        else bool(args.pool)
    )

    return _append_custom_entry(target, prov, model_id, name, alias=alias, pooled=pooled)


def _collision(target: Path, existing: dict, name: str, alias: str | None) -> str | None:
    """The error message for a name or alias already in the file, or ``None``."""
    taken = {
        str(e["name"])
        for section in ("llms", "custom")
        for e in existing.get(section, [])
        if isinstance(e, dict) and e.get("name")
    }
    if name in taken:
        hint = " — use --name" if alias is None else " (that catalog model is already in the file)"
        return f"error: an entry named '{name}' already exists in {target}{hint}"
    aliases = {
        str(e["alias"])
        for e in existing.get("custom", [])
        if isinstance(e, dict) and e.get("alias")
    }
    if alias is not None and alias in aliases:
        return f"error: alias '{alias}' is already used in {target}"
    return None


def _append_custom_entry(  # noqa: PLR0913
    target: Path,
    prov: dict,
    model_id: str,
    name: str,
    *,
    alias: str | None,
    pooled: bool,
) -> int:
    """Append one ``[[custom]]`` block (plus its ``[keys]`` help if the ref is new)
    to ``target``, preserving the rest of the file. Refuses a name or alias collision."""
    try:
        existing = tomllib.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    except (tomllib.TOMLDecodeError, OSError) as exc:
        print(f"error: cannot read existing {target}: {exc}", file=sys.stderr)
        return 1
    clash = _collision(target, existing, name, alias)
    if clash is not None:
        print(clash, file=sys.stderr)
        return 1

    ref = str(prov["api_key_ref"])
    entry: dict = {"alias": alias} if alias is not None else {}
    entry.update(
        {
            "name": name,
            "model": model_id,
            "base_url": str(prov["base_url"]),
            "api_key_ref": ref,
            "pool": pooled,
        },
    )
    block: dict = {"custom": [entry]}
    if ref not in existing.get("keys", {}) and prov.get("key_help"):
        block["keys"] = {ref: {"help": str(prov["key_help"])}}

    rendered = tomli_w.dumps(block).rstrip("\n")
    if target.exists():
        with target.open("a", encoding="utf-8") as fh:
            fh.write("\n" + rendered + "\n")
    else:
        target.write_text(rendered + "\n", encoding="utf-8")

    reach = f"direct({alias!r})" if alias is not None else f"direct(name={name!r})"
    print(f"added [[custom]] '{name}' ({model_id}) to {target} — reach it with {reach}")
    print(f"next: set {ref} (e.g. `llmbroker env {target} >> .env`) and sync your config")
    return 0


def _prompt_name(default: str) -> str:
    return input(f"Entry name [{default}]: ").strip() or default


def _prompt_yes_no(label: str, *, default: bool = False) -> bool:
    raw = input(f"{label} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    return default if not raw else raw in ("y", "yes")


def _cmd_sync(args: argparse.Namespace) -> int:
    preset_path = Path(args.preset)
    if not preset_path.exists():
        print(f"error: no such file: {preset_path}", file=sys.stderr)
        return 1

    async def run() -> None:
        broker = AsyncBroker(args.db)
        try:
            await broker.sync(Registry(preset_path))
        finally:
            await broker.aclose()

    try:
        asyncio.run(run())
    except ImportError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"synced {preset_path} -> {args.db}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m llmbroker")
    sub = parser.add_subparsers(dest="command", required=True)

    env_p = sub.add_parser(
        "env",
        help="emit a .env skeleton of api_key_ref names",
        description=(
            "Emit a .env skeleton of the api_key_ref names a config needs, in file order,"
            " each with its help text. The argument is a local .toml/.json config file or,"
            " when no such file exists, a curated preset name fetched from the catalog —"
            " so `llmbroker env freetier > .env` onboards without any local file."
        ),
    )
    env_p.add_argument("config", help="path to a .toml/.json config file, or a preset name")
    env_p.set_defaults(func=_cmd_env)

    preset_p = sub.add_parser(
        "preset",
        help="print a curated preset TOML to stdout",
        description=(
            "Print a curated preset TOML to stdout. To save: preset freetier > freetier.toml."
            " With --merge FILE, refresh the [[llms]] and [keys] in FILE from the preset while"
            " keeping your [[custom]] models and their keys; a [[custom]] entry carrying an"
            " alias is also re-pointed at whatever the paid catalog now recommends."
        ),
    )
    preset_p.add_argument("name", help="preset name (e.g. freetier)")
    preset_p.add_argument(
        "--merge",
        metavar="FILE",
        help="merge into FILE: refresh [[llms]]/[keys], keep [[custom]] (instead of stdout)",
    )
    preset_p.set_defaults(func=_cmd_preset)

    addm_p = sub.add_parser(
        "add-model",
        help="pick a paid provider/model from the catalog and append it as [[custom]]",
        description=(
            "Pick a paid provider and model from the curated catalog and append a [[custom]]"
            " entry to your config file. Interactive by default; pass --provider and --model"
            " to run non-interactively. The entry follows the catalog's alias — call it as"
            " direct('opus') and `preset <name> --merge` keeps it on the current version."
        ),
    )
    addm_p.add_argument(
        "--into",
        required=True,
        metavar="FILE",
        help="the .toml config to append to",
    )
    addm_p.add_argument(
        "--provider",
        metavar="ID",
        help="provider id (with --model, non-interactive)",
    )
    addm_p.add_argument(
        "--model",
        metavar="ID",
        help="model id (with --provider, non-interactive)",
    )
    addm_p.add_argument(
        "--pin",
        action="store_true",
        help="pin this exact model version: no alias, never rewritten by a catalog refresh",
    )
    addm_p.add_argument(
        "--name",
        metavar="NAME",
        help="entry name for a --pin entry (default: provider id); invalid without --pin",
    )
    addm_p.add_argument(
        "--pool",
        action="store_true",
        help="add to the routed pool (default: direct-only, reached via broker.direct)",
    )
    addm_p.set_defaults(func=_cmd_add_model)

    sync_p = sub.add_parser(
        "sync",
        help="mirror a preset TOML into a DB registry (DB-init workflow)",
        description=(
            "Mirror a preset TOML into a DB registry: add new entries, update"
            " existing ones, delete entries absent from the preset."
        ),
    )
    sync_p.add_argument("preset", help="path to the preset .toml file")
    sync_p.add_argument("db", help="sqlite path or postgresql:// / mongodb:// URL")
    sync_p.set_defaults(func=_cmd_sync)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (EOFError, KeyboardInterrupt):
        print("\naborted", file=sys.stderr)
        return 1

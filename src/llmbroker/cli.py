"""python -m llmbroker <command>.

Subcommands: env (emit .env skeleton), preset (download curated preset TOML, or
--sync it into a config file, refreshing alias-following custom entries from the
paid catalog), add-model (append a paid model from the catalog).

The CLI writes files only. Mirroring a lineup into a DB registry is the host's
own entrypoint calling `broker.sync(...)`, so the connection config and its
secrets stay in one place.
"""

import argparse
import asyncio
import os
import sys
import tempfile
import tomllib
from collections.abc import Callable
from pathlib import Path

import tomli_w

from llmbroker.broker.aliases import alias_targets_for
from llmbroker.broker.keys import KeyProbe
from llmbroker.broker.lineup_file import FileSyncOutcome, entry_block, sync_lineup_file
from llmbroker.broker.merge import SyncSource
from llmbroker.broker.presets import PAID_CATALOG, PRESET_NAME_RE, PresetSource
from llmbroker.broker.report import alias_lines, format_report
from llmbroker.broker.source import lineup_path
from llmbroker.broker.stamps import write_stamp
from llmbroker.exceptions import SyncRefusedError
from llmbroker.home import HOME_ENV_VAR, home_dir
from llmbroker.models import KeyInfo, LLMConfig
from llmbroker.standalone.registry import (
    Registry,
    key_info_from_entry,
    parse_lineup,
    read_lineup,
)
from llmbroker.standalone.secrets import Secrets
from llmbroker.standalone.store import FileStore
from llmbroker.util.atomic import write_atomic


async def _env_data(reg: Registry) -> tuple[list[LLMConfig], dict[str, KeyInfo]]:
    return await reg.load(), await reg.key_info()


def _read_env_data(reg: Registry) -> tuple[list[LLMConfig], dict[str, KeyInfo]] | None:
    """A malformed lineup is the user's own file, so it is reported like every other
    bad argument — the one command onboarding starts with may not end in a traceback."""
    try:
        return asyncio.run(_env_data(reg))
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def _env_source_data(source: str) -> tuple[list[LLMConfig], dict[str, KeyInfo]] | None:
    """Load the ``env`` argument as an existing config file, or as a preset name
    fetched from the catalog — so onboarding needs no local file at all."""
    path = Path(source)
    if path.exists():
        return _read_env_data(Registry(path))
    if not PRESET_NAME_RE.match(source):
        print(
            f"error: no such file: {path} (and {source!r} is not a valid preset name)",
            file=sys.stderr,
        )
        return None
    try:
        text = PresetSource(home_dir()).text(source)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / f"{source}.toml"
        staged.write_text(text, encoding="utf-8")
        return _read_env_data(Registry(staged))


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


def _cmd_preset(args: argparse.Namespace) -> int:
    try:
        text = PresetSource(home_dir()).text(args.name)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.sync is not None:
        return _sync_preset_into(text, args.name, args.sync)
    sys.stdout.write(text)
    return 0


_DB_TARGET_SUFFIXES = (".db", ".sqlite")
_DB_TARGET_SCHEMES = ("sqlite://", "postgresql://", "mongodb://")


async def _sync_target(text: str, name: str, target: Path) -> FileSyncOutcome:
    """The CLI's own merge site: env plus the target's sibling ``.env`` for keys,
    and the sibling ``store/`` for death evidence when the broker put one there."""
    presets = PresetSource(home_dir())
    store_dir = target.parent / "store"
    src = SyncSource(label=name, lineup=parse_lineup(tomllib.loads(text)), preset=True)
    return await sync_lineup_file(
        target,
        src,
        probe=KeyProbe(Secrets(target.parent / ".env")),
        store=FileStore(store_dir) if store_dir.is_dir() else None,
        alias_targets=await alias_targets_for(
            [c.alias for c in read_lineup(target).configs],
            presets,
        ),
    )


def _sync_preset_into(text: str, name: str, raw_target: str) -> int:
    """Merge the fetched preset into the target file and print what it did.

    Keys come from the target's own environment and sibling ``.env`` — the same
    pair a file-configured broker resolves — so what this decides is what the
    application would have decided.
    """
    # The raw argument, not a Path: Path() collapses the "//" of a DSN.
    if raw_target.endswith(_DB_TARGET_SUFFIXES) or raw_target.startswith(_DB_TARGET_SCHEMES):
        print(
            f"error: --sync writes a config file, and {raw_target} is a database."
            " A registry is synced from your own code: build the broker with the factory"
            f' your application already uses and call `await broker.sync("{name}")`'
            " — that keeps the connection config and its secrets in one place",
            file=sys.stderr,
        )
        return 1
    target = Path(raw_target)
    try:
        outcome = asyncio.run(_sync_target(text, name, target))
    except (SyncRefusedError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    # Written, never read: an explicitly typed command always does the thing, but
    # an application on this host shares the clock it just advanced.
    write_stamp(home_dir(), f"{name} {target.resolve()}")
    notices, warnings = alias_lines(outcome.refresh.facts)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    for notice in notices:
        print(notice)
    # Printed on every run, no-ops included: a kept entry and a missing key stay
    # visible in each deploy log until an admin resolves them.
    print(format_report(outcome.report))
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
    home = home_dir()
    if home is None:
        print(
            "error: nowhere to write — llmbroker found no writable directory of its own."
            f" Set {HOME_ENV_VAR} to a directory this process can write to",
            file=sys.stderr,
        )
        return 1
    target = lineup_path(home)
    if args.name and not args.pin:
        print(
            "error: --name is only valid with --pin — an alias entry's name is machine-formed"
            " from the provider and model ids, and rewritten by every catalog refresh",
            file=sys.stderr,
        )
        return 1
    try:
        text = PresetSource(home_dir()).text(PAID_CATALOG)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
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
    return _append_custom_entry(target, prov, model_id, name, alias=alias)


def _collision(target: Path, data: dict, name: str, alias: str | None) -> str | None:
    """The error message for a name or alias already in the lineup, or ``None``."""
    names = {
        str(e["name"])
        for section in ("llms", "custom")
        for e in data.get(section, [])
        if isinstance(e, dict) and e.get("name")
    }
    if name in names:
        hint = " — use --name" if alias is None else " (that catalog model is already in the file)"
        return f"error: an entry named '{name}' already exists in {target}{hint}"
    aliases = {
        str(e["alias"]) for e in data.get("custom", []) if isinstance(e, dict) and e.get("alias")
    }
    if alias is not None and alias in aliases:
        return f"error: alias '{alias}' is already used in {target}"
    return None


def _read_toml(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _append_custom_entry(
    target: Path,
    prov: dict,
    model_id: str,
    name: str,
    *,
    alias: str | None,
) -> int:
    """Append one ``[[custom]]`` block, plus its ``[keys]`` help if the ref is new.

    Preserves the rest of the file, and refuses a name or alias collision.
    """
    try:
        data = _read_toml(target)
    except (tomllib.TOMLDecodeError, OSError) as exc:
        print(f"error: cannot read existing {target}: {exc}", file=sys.stderr)
        return 1
    clash = _collision(target, data, name, alias)
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
        },
    )
    parts = [entry_block("custom", entry)]
    if ref not in data.get("keys", {}) and prov.get("key_help"):
        parts.append(tomli_w.dumps({"keys": {ref: {"help": str(prov["key_help"])}}}).rstrip("\n"))

    if target.exists():
        parts.insert(0, target.read_text(encoding="utf-8").rstrip("\n"))
    target.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(target, "\n\n".join(parts) + "\n")

    reach = f"direct({alias!r})" if alias is not None else f"direct(name={name!r})"
    print(f"added [[custom]] '{name}' ({model_id}) to {target} — reach it with {reach}")
    print(f"next: set {ref} (e.g. `llmbroker env {target} >> .env`) and sync your config")
    return 0


def _prompt_name(default: str) -> str:
    return input(f"Entry name [{default}]: ").strip() or default


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
            "Print a curated preset TOML to stdout. With --sync FILE, regenerate FILE from"
            " the preset: the pool models, the keys they need, your own models carried over,"
            " and every alias-following entry re-pointed at whatever the paid catalog now"
            " recommends. A model the preset dropped stays in FILE until a replacement you"
            " can actually call arrives, so an update never shrinks your pool. FILE is"
            " written by llmbroker and is not meant to be edited by hand."
        ),
    )
    preset_p.add_argument("name", help="preset name (e.g. freetier)")
    preset_p.add_argument(
        "--sync",
        metavar="FILE",
        help="sync into FILE: regenerate it from the preset (instead of stdout)",
    )
    preset_p.set_defaults(func=_cmd_preset)

    addm_p = sub.add_parser(
        "add-model",
        help="pick a paid provider/model from the catalog and add it to your lineup",
        description=(
            "Pick a paid provider and model from the curated catalog and add it to this"
            " installation's lineup. Interactive by default; pass --provider and --model"
            " to run non-interactively. The entry follows the catalog's alias — call it as"
            " direct('opus') and a refresh keeps it on the current version; --pin writes a"
            f" version-pinned entry no refresh touches. The lineup lives in llmbroker's own"
            f" directory (set {HOME_ENV_VAR} to move it). A broker whose registry is a"
            " database takes its own models from direct=[...] in your code instead."
        ),
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
    addm_p.set_defaults(func=_cmd_add_model)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (EOFError, KeyboardInterrupt):
        print("\naborted", file=sys.stderr)
        return 1

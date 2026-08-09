"""python -m llmbroker <command>.

Subcommands: env (emit .env skeleton), add-model (append a paid model from the
catalog).

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

from llmbroker.broker.lineup_file import entry_block
from llmbroker.broker.presets import PAID_CATALOG, PRESET_NAME_RE, PresetSource
from llmbroker.broker.source import lineup_path
from llmbroker.home import HOME_ENV_VAR, home_dir
from llmbroker.models import KeyInfo, LLMConfig
from llmbroker.standalone.registry import Registry, key_info_from_entry
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


def _env_own_lineup() -> tuple[list[LLMConfig], dict[str, KeyInfo]] | None:
    """The keys this installation's own lineup needs — the pool it already follows
    plus whatever ``add-model`` put beside it."""
    home = home_dir()
    if home is None:
        print(
            "error: llmbroker found no writable directory of its own, so this"
            " installation has no lineup to read. Name a curated preset instead"
            f" (e.g. `llmbroker env freetier`), or set {HOME_ENV_VAR}",
            file=sys.stderr,
        )
        return None
    path = lineup_path(home)
    # Onboarding runs this before any broker has ever run, so the file is normally
    # absent on the first go: an empty skeleton would read as "no keys needed".
    if not path.exists():
        print(
            f"error: this installation has no lineup yet ({path} does not exist) — a"
            " broker writes it on its first run. Name a curated preset to get the keys"
            " now: `llmbroker env freetier`",
            file=sys.stderr,
        )
        return None
    return _read_env_data(Registry(path))


def _env_preset(name: str) -> tuple[list[LLMConfig], dict[str, KeyInfo]] | None:
    """The keys a curated preset needs, fetched from the catalog — so onboarding
    needs no local lineup at all."""
    if not PRESET_NAME_RE.match(name):
        print(f"error: {name!r} is not a valid preset name", file=sys.stderr)
        return None
    try:
        text = PresetSource(home_dir()).text(name)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / f"{name}.toml"
        staged.write_text(text, encoding="utf-8")
        return _read_env_data(Registry(staged))


def _cmd_env(args: argparse.Namespace) -> int:
    # llms in file order; infos maps ref -> KeyInfo only for refs with a [keys] entry.
    data = _env_own_lineup() if args.preset is None else _env_preset(args.preset)
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
    print(f"next: set {ref} (e.g. `llmbroker env >> .env`)")
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
            "Emit a .env skeleton of the api_key_ref names a lineup needs, in file order,"
            " each with its help text. With no argument it reads this installation's own"
            " lineup; name a curated preset instead — `llmbroker env freetier > .env` —"
            " to onboard before anything local exists, or to read the curated keys on an"
            " installation whose registry is a database."
        ),
    )
    env_p.add_argument(
        "preset",
        nargs="?",
        help="curated preset name (e.g. freetier); omit to read this installation's lineup",
    )
    env_p.set_defaults(func=_cmd_env)

    addm_p = sub.add_parser(
        "add-model",
        help="pick a paid provider/model from the catalog and add it to your lineup",
        description=(
            "Pick a paid provider and model from the curated catalog and add it to this"
            " installation's lineup. Interactive by default; pass --provider and --model"
            " to run non-interactively. The entry follows the catalog's alias — call it as"
            " direct('opus') and a refresh keeps it on the current version; --pin writes a"
            f" version-pinned entry no refresh touches. The lineup lives in llmbroker's own"
            f" directory (set {HOME_ENV_VAR} to move it). A host that owns its own registry"
            " declares its models with direct=[...] in code instead."
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

"""python -m llmbroker <command>.

Subcommands: env (emit .env skeleton), preset (download curated preset TOML,
or --merge it into a file), add-model (append a paid model from the catalog),
sync (mirror a preset TOML into a DB registry — DB-init workflow).
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


def _merge_preset(preset_text: str, name: str, target: Path) -> int:
    """Refresh the managed ``[[llms]]`` + ``[keys]`` in ``target`` from a fresh preset,
    keeping the user's ``[[custom]]`` models and their keys. Managed comments come from
    the preset verbatim; the re-emitted ``[[custom]]`` tail loses inline comments."""
    if target.suffix.lower() != ".toml":
        print(f"error: --merge target must be a .toml file, got {target}", file=sys.stderr)
        return 1
    try:
        existing = tomllib.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    except (tomllib.TOMLDecodeError, OSError) as exc:
        print(f"error: cannot read existing {target}: {exc}", file=sys.stderr)
        return 1

    preset_key_refs = set(tomllib.loads(preset_text).get("keys", {}))
    custom_entries = [e for e in existing.get("custom", []) if isinstance(e, dict)]
    custom_refs = {e["api_key_ref"] for e in custom_entries if e.get("api_key_ref")}
    existing_keys = existing.get("keys", {})
    custom_keys = {
        ref: existing_keys[ref]
        for ref in custom_refs
        if ref in existing_keys and ref not in preset_key_refs
    }

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


def _prompt_choice(label: str, items: list[dict], key: str) -> dict | None:
    """Print a numbered menu of ``items`` (shown by ``key`` or its own name) and read a pick."""
    for i, item in enumerate(items, 1):
        print(f"  {i}. {item.get(key) or item.get('model') or item.get('id')}")
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
) -> tuple[dict, str] | None:
    if provider_id is None or model_id is None:
        print("error: --provider and --model must be given together", file=sys.stderr)
        return None
    prov = next((p for p in providers if p.get("id") == provider_id), None)
    if prov is None:
        have = ", ".join(str(p.get("id")) for p in providers)
        print(f"error: unknown provider '{provider_id}' (have: {have})", file=sys.stderr)
        return None
    models = [
        str(m["model"]) for m in prov.get("models", []) if isinstance(m, dict) and m.get("model")
    ]
    if model_id not in models:
        have = ", ".join(models)
        print(f"error: unknown model '{model_id}' (have: {have})", file=sys.stderr)
        return None
    return prov, model_id


def _select_interactive(providers: list[dict]) -> tuple[dict, str] | None:
    prov = _prompt_choice("Pick a provider", providers, "label")
    if prov is None:
        return None
    models = [m for m in prov.get("models", []) if isinstance(m, dict) and m.get("model")]
    if not models:
        print(f"error: provider '{prov.get('id')}' has no usable models", file=sys.stderr)
        return None
    model = _prompt_choice("Pick a model", models, "label")
    if model is None:
        return None
    return prov, str(model["model"])


def _select_provider_model(
    providers: list[dict],
    provider_id: str | None,
    model_id: str | None,
) -> tuple[dict, str] | None:
    """Resolve (provider, model id) from flags, or interactively. ``None`` on error."""
    if provider_id is not None or model_id is not None:
        return _select_from_flags(providers, provider_id, model_id)
    return _select_interactive(providers)


def _cmd_add_model(args: argparse.Namespace) -> int:  # noqa: PLR0911
    target = Path(args.into)
    if target.suffix.lower() != ".toml":
        print(f"error: --into target must be a .toml file, got {target}", file=sys.stderr)
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
    prov, model_id = selection
    if not (prov.get("base_url") and prov.get("api_key_ref")):
        print(
            f"error: catalog entry for '{prov.get('id')}' is incomplete"
            " (needs base_url and api_key_ref)",
            file=sys.stderr,
        )
        return 1

    if interactive:
        name = _prompt_name(args.name or str(prov["id"]))
        pooled = _prompt_yes_no("Add to the pool (failover)?", default=bool(args.pool))
    else:
        name = (args.name or str(prov["id"])).strip()
        pooled = bool(args.pool)

    return _append_custom_entry(target, prov, model_id, name, pooled=pooled)


def _append_custom_entry(
    target: Path,
    prov: dict,
    model_id: str,
    name: str,
    *,
    pooled: bool,
) -> int:
    """Append one ``[[custom]]`` block (plus its ``[keys]`` help if the ref is new)
    to ``target``, preserving the rest of the file. Refuses a name collision."""
    try:
        existing = tomllib.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    except (tomllib.TOMLDecodeError, OSError) as exc:
        print(f"error: cannot read existing {target}: {exc}", file=sys.stderr)
        return 1
    taken = {
        str(e["name"])
        for section in ("llms", "custom")
        for e in existing.get(section, [])
        if isinstance(e, dict) and e.get("name")
    }
    if name in taken:
        print(
            f"error: an entry named '{name}' already exists in {target} — use --name",
            file=sys.stderr,
        )
        return 1

    ref = str(prov["api_key_ref"])
    block: dict = {
        "custom": [
            {
                "name": name,
                "base_url": str(prov["base_url"]),
                "model": model_id,
                "api_key_ref": ref,
                "pool": pooled,
            },
        ],
    }
    if ref not in existing.get("keys", {}) and prov.get("key_help"):
        block["keys"] = {ref: {"help": str(prov["key_help"])}}

    rendered = tomli_w.dumps(block).rstrip("\n")
    if target.exists():
        with target.open("a", encoding="utf-8") as fh:
            fh.write("\n" + rendered + "\n")
    else:
        target.write_text(rendered + "\n", encoding="utf-8")

    print(f"added [[custom]] '{name}' ({model_id}) to {target}")
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
            " keeping your [[custom]] models and their keys."
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
            " to run non-interactively."
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
    addm_p.add_argument("--name", metavar="NAME", help="entry name (default: provider id)")
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

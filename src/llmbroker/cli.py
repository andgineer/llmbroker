"""python -m llmbroker <command>.

Subcommands: env (emit .env skeleton), preset (download curated preset TOML),
sync (mirror a preset TOML into a DB registry — DB-init workflow).
"""

import argparse
import asyncio
import os
import re
import sys
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


def _cmd_env(args: argparse.Namespace) -> int:
    toml_path = Path(args.config)
    if not toml_path.exists():
        print(f"error: no such file: {toml_path}", file=sys.stderr)
        return 1

    # llms in file order; infos maps ref -> KeyInfo only for refs with a [keys] entry.
    configs, infos = asyncio.run(_env_data(Registry(toml_path)))

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


def _cmd_preset(args: argparse.Namespace) -> int:  # noqa: PLR0911
    name = args.name
    if not _PRESET_NAME_RE.match(name):
        print(
            f"error: invalid preset name '{name}' (use letters, digits, hyphens, underscores)",
            file=sys.stderr,
        )
        return 1
    url = _PRESET_URL.format(name=name)

    try:
        with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 - name validated above
            content = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == HTTPStatus.NOT_FOUND:
            print(f"error: preset '{name}' not found in catalog", file=sys.stderr)
        else:
            print(f"error: HTTP {exc.code} fetching {url}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"error: {exc.reason}", file=sys.stderr)
        return 1

    try:
        text = content.decode()
    except UnicodeDecodeError:
        print(f"error: downloaded content for '{name}' is not valid UTF-8", file=sys.stderr)
        return 1
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        print(f"error: downloaded content for '{name}' is not valid TOML", file=sys.stderr)
        return 1

    if args.merge is not None:
        return _merge_preset(text, name, Path(args.merge))

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

    env_p = sub.add_parser("env", help="emit a .env skeleton of api_key_ref names")
    env_p.add_argument("config", help="path to a .toml config file")
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
    return args.func(args)

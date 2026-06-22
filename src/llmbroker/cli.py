"""python -m llmbroker <command>.

Subcommands: env (emit .env skeleton), preset (download curated preset TOML).
"""

import argparse
import re
import sys
import tomllib
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path

_PRESET_URL = "https://raw.githubusercontent.com/andgineer/llmbroker/main/presets/{name}.toml"
_PRESET_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _api_key_refs(toml_path: Path) -> list[str]:
    with toml_path.open("rb") as fh:
        data = tomllib.load(fh)
    refs: list[str] = []
    for entry in data.get("llms", []):
        ref = entry.get("api_key_ref")
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def _cmd_env(args: argparse.Namespace) -> int:
    toml_path = Path(args.config)
    if not toml_path.exists():
        print(f"error: no such file: {toml_path}", file=sys.stderr)
        return 1
    lines = [f"{ref}=" for ref in _api_key_refs(toml_path)]
    print("\n".join(lines))
    return 0


def _cmd_preset(args: argparse.Namespace) -> int:
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

    sys.stdout.write(text)
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
            "Print a curated preset TOML to stdout. To save: preset freetier > freetier.toml"
        ),
    )
    preset_p.add_argument("name", help="preset name (e.g. freetier, smart-freetier)")
    preset_p.set_defaults(func=_cmd_preset)

    args = parser.parse_args(argv)
    return args.func(args)

"""python -m llmbroker <command>.

Subcommands: env (emit .env skeleton), preset (download curated preset TOML).
"""

import argparse
import os
import re
import sys
import tomllib
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path

from llmbroker.models import EffortLevel, KeyInfo, ValueLevel
from llmbroker.standalone.registry import key_info_from_entry

_PRESET_URL = "https://raw.githubusercontent.com/andgineer/llmbroker/main/presets/{name}.toml"
_PRESET_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _api_key_refs(data: dict) -> list[str]:
    refs: list[str] = []
    for entry in data.get("llms", []):
        ref = entry.get("api_key_ref")
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def _daily_cap(data: dict, ref: str) -> int | None:
    """Max advertised ``rpd`` across this ref's ``[[llms]]`` rows; ``None`` if none set."""
    caps = [
        entry["rate_limit"]["rpd"]
        for entry in data.get("llms", [])
        if entry.get("api_key_ref") == ref
        and isinstance(entry.get("rate_limit"), dict)
        and isinstance(entry["rate_limit"].get("rpd"), int)
    ]
    return max(caps) if caps else None


def _onboarding_sort_key(ref: str, info: KeyInfo, cap: int | None) -> tuple:
    """Easiest+most-valuable first; unknown effort/value sort last; larger cap sorts earlier."""
    effort_idx = (
        list(EffortLevel).index(info.effort) if info.effort is not None else len(EffortLevel)
    )
    value_idx = list(ValueLevel).index(info.value) if info.value is not None else len(ValueLevel)
    return (effort_idx, value_idx, -(cap or 0), ref)


def _annotation(info: KeyInfo, cap: int | None) -> str | None:
    parts = []
    if info.effort is not None:
        parts.append(f"effort={info.effort.value}")
    if info.value is not None:
        parts.append(f"value={info.value.value}")
    if cap is not None:
        parts.append(f"daily cap={cap}")
    return ", ".join(parts) if parts else None


def _cmd_env(args: argparse.Namespace) -> int:
    toml_path = Path(args.config)
    if not toml_path.exists():
        print(f"error: no such file: {toml_path}", file=sys.stderr)
        return 1
    with toml_path.open("rb") as fh:
        data = tomllib.load(fh)
    raw_keys = data.get("keys", {})
    if not isinstance(raw_keys, dict):
        raw_keys = {}

    refs = _api_key_refs(data)
    infos = {ref: key_info_from_entry(ref, raw_keys.get(ref)) for ref in refs}
    caps = {ref: _daily_cap(data, ref) for ref in refs}
    refs.sort(key=lambda ref: _onboarding_sort_key(ref, infos[ref], caps[ref]))

    lines: list[str] = []
    for ref in refs:
        info = infos[ref]
        if info.help.strip():
            for i, line in enumerate(info.help.splitlines()):
                lines.append(f"# {ref} — {line}" if i == 0 else f"# {line}")
        annotation = _annotation(info, caps[ref])
        if annotation is not None:
            lines.append(f"# {annotation}")
        if ref in os.environ:
            lines.append(f"# {ref} already set")
        else:
            lines.append(f"{ref}=")
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
    preset_p.add_argument("name", help="preset name (e.g. freetier)")
    preset_p.set_defaults(func=_cmd_preset)

    args = parser.parse_args(argv)
    return args.func(args)

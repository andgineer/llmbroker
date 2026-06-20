"""python -m llmbroker <command>.

P1 ships ``env`` (emit a .env skeleton of api_key_ref names).
"""

import argparse
import sys
import tomllib
from pathlib import Path


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m llmbroker")
    sub = parser.add_subparsers(dest="command", required=True)

    env_p = sub.add_parser("env", help="emit a .env skeleton of api_key_ref names")
    env_p.add_argument("config", help="path to a .toml config file")
    env_p.set_defaults(func=_cmd_env)

    args = parser.parse_args(argv)
    return args.func(args)

"""python -m llmbroker <command>.

P1 ships ``env`` (emit a .env skeleton of api_key_ref names) and ``sync``
(reconcile a TOML into a sqlite DB). Both operate offline on local paths.
"""

import argparse
import asyncio
import sys
import tomllib
from pathlib import Path

import llmbroker.sqlite
from llmbroker.broker import AsyncBroker
from llmbroker.registry import Registry


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


def _cmd_sync(args: argparse.Namespace) -> int:
    target = args.into
    prefix = "sqlite:"
    if not target.startswith(prefix):
        print(f"error: --into must be sqlite:<path>, got {target!r}", file=sys.stderr)
        return 1
    db_path = target[len(prefix) :]

    async def _run() -> None:
        broker = AsyncBroker(registry=llmbroker.sqlite.Registry(db_path))
        async with broker:
            await broker.sync_configs(Registry(args.config), policy=args.policy)

    asyncio.run(_run())
    print(f"synced {args.config} into {db_path} (policy={args.policy})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m llmbroker")
    sub = parser.add_subparsers(dest="command", required=True)

    env_p = sub.add_parser("env", help="emit a .env skeleton of api_key_ref names")
    env_p.add_argument("config", help="path to a .toml config file")
    env_p.set_defaults(func=_cmd_env)

    sync_p = sub.add_parser("sync", help="reconcile a TOML into a sqlite DB")
    sync_p.add_argument("config", help="path to a .toml config file")
    sync_p.add_argument("--into", required=True, help="sqlite:<path>")
    sync_p.add_argument(
        "--policy",
        choices=["mirror", "add", "if_empty"],
        default="mirror",
    )
    sync_p.set_defaults(func=_cmd_sync)

    args = parser.parse_args(argv)
    return args.func(args)

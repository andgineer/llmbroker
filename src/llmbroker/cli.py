"""python -m llmbroker <command> — ``env`` and ``list``. The CLI reads: a model list
is filled by a sync, and a model reached by name is declared in the host's own code."""

import argparse
import asyncio
import sys
import tempfile
import tomllib
from pathlib import Path

from llmbroker.broker.presets import PAID_CATALOG, POOL_PRESET, PRESET_NAME_RE, PresetSource
from llmbroker.home import home_dir
from llmbroker.models import KeyInfo, LLMConfig
from llmbroker.standalone.registry import Registry, key_info_from_entry


async def _env_data(reg: Registry) -> tuple[list[LLMConfig], dict[str, KeyInfo]]:
    return await reg.load(), await reg.key_info()


def _env_preset(name: str) -> tuple[list[LLMConfig], dict[str, KeyInfo]] | None:
    """The keys a curated preset needs, fetched from the catalog — so onboarding
    needs nothing local at all. A malformed preset is reported like every other bad
    argument: the one command onboarding starts with may not end in a traceback."""
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
        try:
            return asyncio.run(_env_data(Registry(staged)))
        except (ValueError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return None


def _cmd_env(args: argparse.Namespace) -> int:
    # llms in declaration order; infos maps ref -> KeyInfo only for refs with a [keys] entry.
    data = _env_preset(args.preset)
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
        lines.append(f"{ref}=")
    print("\n".join(lines))
    return 0


def _preset_data(name: str) -> dict | None:
    """One curated preset, parsed. ``None`` after reporting why it could not be read."""
    try:
        return tomllib.loads(PresetSource(home_dir()).text(name))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None


def _pool_lines(data: dict) -> list[str]:
    return [
        f"pool {entry['name']} {entry.get('model', '')} {entry.get('base_url', '')}"
        f" {entry.get('api_key_ref', '')}"
        for entry in data.get("llms", [])
        if isinstance(entry, dict) and entry.get("name")
    ]


def _direct_lines(data: dict) -> list[str]:
    """One line per paid model: the alias ``direct=`` takes, then the four fields a
    version-pinned declaration states for itself. ``-`` where the catalog has no alias."""
    lines: list[str] = []
    for prov in data.get("provider", []):
        if not isinstance(prov, dict):
            continue
        for model in prov.get("models", []):
            if not isinstance(model, dict) or not model.get("model"):
                continue
            lines.append(
                f"direct {model.get('alias') or '-'} {prov.get('id', '')} {model['model']}"
                f" {prov.get('base_url', '')} {prov.get('api_key_ref', '')}",
            )
    return lines


def _cmd_list(_args: argparse.Namespace) -> int:
    pool = _preset_data(POOL_PRESET)
    paid = _preset_data(PAID_CATALOG)
    if pool is None or paid is None:
        return 1
    print("\n".join([*_pool_lines(pool), *_direct_lines(paid)]))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m llmbroker")
    sub = parser.add_subparsers(dest="command", required=True)

    env_p = sub.add_parser(
        "env",
        help="emit a .env skeleton of the api_key_ref names a curated preset needs",
        description=(
            "Emit a .env skeleton of the api_key_ref names a curated model list needs, in"
            " declaration order, each with the help text saying where that key comes from"
            " — `llmbroker env freetier > .env`."
        ),
    )
    env_p.add_argument("preset", help="curated preset name (e.g. freetier)")
    env_p.set_defaults(func=_cmd_env)

    list_p = sub.add_parser(
        "list",
        help="show the curated model lists — the routed pool and the paid models",
        description=(
            "Show what the curated model lists carry, one model per line and nothing"
            " written. A `pool` line is a model a sync routes over anonymously. A `direct`"
            " line is a paid model you reach by name: its first field is the alias to pass"
            ' as direct=["opus"], and the rest is the provider id, model id, base_url and'
            " api_key_ref a version-pinned direct=[LLMConfig(...)] states for itself."
        ),
    )
    list_p.set_defaults(func=_cmd_list)

    args = parser.parse_args(argv)
    return args.func(args)

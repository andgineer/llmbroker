"""python -m llmbroker <command> — ``env`` and ``list``. The CLI reads: a model list
is filled by a sync, and a model reached by name is declared in the host's own code."""

import argparse
import asyncio
import os
import sys
import tempfile
import tomllib
from pathlib import Path

from llmbroker.broker.presets import PAID_CATALOG, POOL_PRESET, PRESET_NAME_RE, PresetSource
from llmbroker.broker.source import lineup_path
from llmbroker.home import HOME_ENV_VAR, home_dir
from llmbroker.models import KeyInfo, LLMConfig
from llmbroker.standalone.registry import Registry, key_info_from_entry


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
    """The keys the model list this installation already follows needs."""
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

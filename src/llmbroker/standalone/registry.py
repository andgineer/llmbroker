"""File-backed registry: ``.toml`` / ``.json`` of ``[[llms]]`` rows, no secrets."""

import json
import tomllib
from pathlib import Path

from llmbroker.models import KeyInfo, LLMConfig, check_unique_aliases, check_weight


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _weight_from_entry(raw: object, name: object) -> float:
    """A weight the curator wrote by hand: refuse a bad one loudly, unlike a stored
    row, which is clamped so one malformed record cannot stop a broker starting."""
    if raw is None:
        return 0.0
    # bool is an int subclass, and `weight = true` says nothing about quality.
    numeric = float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None
    try:
        if numeric is None:
            raise ValueError(f"weight must be a number, got {raw!r}")
        check_weight(numeric)
    except ValueError as exc:
        raise ValueError(f"Registry: entry {name!r}: {exc}") from exc
    return numeric


def config_from_entry(entry: dict, *, custom: bool) -> LLMConfig | None:
    name = entry.get("name")
    base_url = entry.get("base_url")
    alias = entry.get("alias")
    if alias is not None and not custom:
        raise ValueError(
            f"Registry: [[llms]] entry {name!r} carries an 'alias' — aliases belong to"
            " [[custom]] entries only; preset-managed pool models are anonymous",
        )
    if not name or not base_url:
        return None
    return LLMConfig(
        name=str(name),
        base_url=str(base_url),
        model=str(entry.get("model", "")),
        api_key_ref=str(entry.get("api_key_ref", "")),
        parallel=_int_or_none(entry.get("parallel")),
        custom=custom,
        alias=str(alias) if alias is not None else None,
        weight=_weight_from_entry(entry.get("weight"), name),
    )


ALIAS_NAME_HINT = (
    "an alias entry's name is machine-formed again on every refresh, so renaming it will"
    " not stick — drop its 'alias' to pin it instead"
)


def _check_unique_names(configs: list[LLMConfig]) -> None:
    """Refuse a name carried twice across ``[[llms]]`` and ``[[custom]]``."""
    seen: dict[str, LLMConfig] = {}
    for cfg in configs:
        first = seen.get(cfg.name)
        if first is not None:
            hint = f"; {ALIAS_NAME_HINT}" if first.alias or cfg.alias else ""
            raise ValueError(
                f"Registry: duplicate name {cfg.name!r} — a name identifies exactly one"
                f" entry, across [[llms]] and [[custom]] alike{hint}",
            )
        seen[cfg.name] = cfg


def key_info_from_entry(ref: str, raw: object) -> KeyInfo:
    """Parse one ``[keys.REF]`` entry; a bare string is the help-only form."""
    if isinstance(raw, str):
        return KeyInfo(api_key_ref=ref, help=raw, extra={})
    if not isinstance(raw, dict):
        return KeyInfo(api_key_ref=ref, help="", extra={})
    help_text = raw.get("help")
    extra = {str(k): str(v) for k, v in raw.items() if k != "help"}
    return KeyInfo(
        api_key_ref=ref,
        help=help_text if isinstance(help_text, str) else "",
        extra=extra,
    )


def _key_infos(data: dict) -> dict[str, KeyInfo]:
    raw = data.get("keys", {})
    if not isinstance(raw, dict):
        # ValueError, not TypeError: see the entry check in parse_lineup.
        raise ValueError(  # noqa: TRY004
            f"Registry: [keys] is {type(raw).__name__}, not a table of api_key_ref"
            " sections — this is where a human is told how to obtain each key",
        )
    return {str(ref): key_info_from_entry(str(ref), val) for ref, val in raw.items()}


def parse_lineup(data: dict) -> tuple[list[LLMConfig], dict[str, KeyInfo]]:
    """The one reader of a lineup: ``[[llms]]`` then ``[[custom]]`` in file order,
    plus the ``[keys]`` metadata. Whether a lineup is valid is decided only here."""
    configs: list[LLMConfig] = []
    for section, custom in (("llms", False), ("custom", True)):
        for position, entry in enumerate(data.get(section, []), start=1):
            if not isinstance(entry, dict):
                # ValueError, not TypeError: a malformed lineup must stay inside the
                # error type a background refresh catches rather than kill the process.
                raise ValueError(  # noqa: TRY004
                    f"Registry: [[{section}]] entry {position} is {type(entry).__name__},"
                    " not a table",
                )
            cfg = config_from_entry(entry, custom=custom)
            if cfg is not None:
                configs.append(cfg)
    _check_unique_names(configs)
    check_unique_aliases(configs)
    return configs, _key_infos(data)


def _read_data(path: Path) -> dict:
    """Parse the file into a dict; ``{}`` if missing, ``ValueError`` if unsupported."""
    if not path.exists():
        return {}
    suffix = path.suffix.lower()
    if suffix == ".toml":
        with path.open("rb") as fh:
            return tomllib.load(fh)
    if suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    raise ValueError(
        f"Registry: unsupported config extension {suffix!r} for {path} — expected .toml or .json",
    )


class Registry:
    """File-backed read-only registry — ``.toml`` / ``.json`` by extension."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    async def load(self) -> list[LLMConfig]:
        """The file's ``[[llms]]`` and ``[[custom]]`` entries, validated."""
        return parse_lineup(_read_data(self._path))[0]

    async def key_info(self) -> dict[str, KeyInfo]:
        """Per-provider onboarding metadata from the ``[keys]`` table, keyed by ``api_key_ref``."""
        return _key_infos(_read_data(self._path))

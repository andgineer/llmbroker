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


def _check_unique_names(configs: list[LLMConfig]) -> None:
    """Refuse a name carried twice across ``[[llms]]`` and ``[[custom]]``.

    Every downstream store keys on the name — a DB registry's primary key, the
    live pool's slot map — so a duplicate is not an ambiguity to resolve later
    but an entry silently lost at the next sync.
    """
    seen: set[str] = set()
    for cfg in configs:
        if cfg.name in seen:
            raise ValueError(
                f"Registry: duplicate name {cfg.name!r} — a name identifies exactly one"
                " entry, across [[llms]] and [[custom]] alike",
            )
        seen.add(cfg.name)


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
        """Load ``[[llms]]`` (preset-managed) and ``[[custom]]`` (user-owned) entries.

        The two arrays are parsed identically; ``[[custom]]`` entries are flagged
        ``custom=True``, which keeps them out of the routed pool and out of every
        sync's reach. Only ``[[custom]]`` entries may carry an ``alias``, unique
        across the file; names are unique across both arrays.
        """
        data = _read_data(self._path)
        result: list[LLMConfig] = []
        for entry in data.get("llms", []):
            cfg = config_from_entry(entry, custom=False)
            if cfg is not None:
                result.append(cfg)
        for entry in data.get("custom", []):
            cfg = config_from_entry(entry, custom=True)
            if cfg is not None:
                result.append(cfg)
        _check_unique_names(result)
        check_unique_aliases(result)
        return result

    async def key_info(self) -> dict[str, KeyInfo]:
        """Per-provider onboarding metadata from the ``[keys]`` table, keyed by ``api_key_ref``."""
        raw = _read_data(self._path).get("keys", {})
        if not isinstance(raw, dict):
            return {}
        return {str(ref): key_info_from_entry(str(ref), val) for ref, val in raw.items()}

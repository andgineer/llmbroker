"""File-backed registry: ``.toml`` / ``.json`` of ``[[llms]]`` rows, no secrets."""

import json
import tomllib
from pathlib import Path

from llmbroker.models import KeyInfo, LLMConfig


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _config_from_entry(entry: dict, *, custom: bool) -> LLMConfig | None:
    name = entry.get("name")
    base_url = entry.get("base_url")
    if not name or not base_url:
        return None
    raw_pool = entry.get("pool", True)
    return LLMConfig(
        name=str(name),
        base_url=str(base_url),
        model=str(entry.get("model", "")),
        api_key_ref=str(entry.get("api_key_ref", "")),
        parallel=_int_or_none(entry.get("parallel")),
        pooled=raw_pool if isinstance(raw_pool, bool) else True,
        custom=custom,
    )


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
        ``custom=True`` so ``sync`` never prunes them. Both honor a per-entry
        ``pool`` flag, so a custom model can join the pool or stay direct-only.
        """
        data = _read_data(self._path)
        result: list[LLMConfig] = []
        for entry in data.get("llms", []):
            cfg = _config_from_entry(entry, custom=False)
            if cfg is not None:
                result.append(cfg)
        for entry in data.get("custom", []):
            cfg = _config_from_entry(entry, custom=True)
            if cfg is not None:
                result.append(cfg)
        return result

    async def key_info(self) -> dict[str, KeyInfo]:
        """Per-provider onboarding metadata from the ``[keys]`` table, keyed by ``api_key_ref``."""
        raw = _read_data(self._path).get("keys", {})
        if not isinstance(raw, dict):
            return {}
        return {str(ref): key_info_from_entry(str(ref), val) for ref, val in raw.items()}

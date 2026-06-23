"""File-backed registry: ``.toml`` / ``.json`` of ``[[llms]]`` rows, no secrets."""

import json
import tomllib
from pathlib import Path

from llmbroker.models import LLMConfig


def _config_from_entry(entry: dict) -> LLMConfig | None:
    name = entry.get("name")
    base_url = entry.get("base_url")
    if not name or not base_url:
        return None
    return LLMConfig(
        name=str(name),
        base_url=str(base_url),
        model=str(entry.get("model", "")),
        api_key_ref=str(entry.get("api_key_ref", "")),
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

    async def load(self, user_id: int | str | None = None) -> list[LLMConfig]:  # noqa: ARG002
        data = _read_data(self._path)
        result: list[LLMConfig] = []
        for entry in data.get("llms", []):
            cfg = _config_from_entry(entry)
            if cfg is not None:
                result.append(cfg)
        return result

    async def key_help(self) -> dict[str, str]:
        """Per-key markdown help from the ``[keys]`` table, keyed by ``api_key_ref``."""
        raw = _read_data(self._path).get("keys", {})
        if not isinstance(raw, dict):
            return {}
        return {str(ref): text for ref, text in raw.items() if isinstance(text, str)}

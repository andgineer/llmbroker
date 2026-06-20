"""Registry port protocols and the zero-dependency file battery.

``Registry`` reads a ``.toml`` or ``.json`` file of ``[[llms]]`` sections and
returns pure ``LLMConfig`` rows (no secret). Mutable backends (sqlite, …)
implement ``MutableRegistryProtocol``.
"""

import json
import tomllib
from pathlib import Path
from typing import Protocol, runtime_checkable

from llmbroker.models import LLMConfig


class RegistryProtocol(Protocol):
    async def load(self) -> list[LLMConfig]: ...


@runtime_checkable
class MutableRegistryProtocol(RegistryProtocol, Protocol):
    async def get(self, name: str) -> LLMConfig | None: ...
    async def add(self, cfg: LLMConfig) -> None: ...
    async def update(self, cfg: LLMConfig) -> None: ...
    async def remove(self, name: str) -> None: ...


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


class Registry:
    """File-backed read-only registry — ``.toml`` / ``.json`` by extension."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    async def load(self) -> list[LLMConfig]:
        path = self._path
        if not path.exists():
            return []
        suffix = path.suffix.lower()
        if suffix == ".toml":
            with path.open("rb") as fh:
                data = tomllib.load(fh)
        elif suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            raise ValueError(
                f"Registry: unsupported config extension {suffix!r} for {path} —"
                " expected .toml or .json",
            )
        result: list[LLMConfig] = []
        for entry in data.get("llms", []):
            cfg = _config_from_entry(entry)
            if cfg is not None:
                result.append(cfg)
        return result

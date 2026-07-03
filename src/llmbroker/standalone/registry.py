"""File-backed registry: ``.toml`` / ``.json`` of ``[[llms]]`` rows, no secrets."""

import contextlib
import json
import os
import tempfile
import tomllib
from pathlib import Path

from llmbroker.models import (
    EffortLevel,
    KeyInfo,
    LLMConfig,
    LLMProfile,
    RateLimit,
    ValueLevel,
    check_user_id,
)


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _rate_limit_from_entry(raw: object) -> RateLimit | None:
    if not isinstance(raw, dict):
        return None
    return RateLimit(
        rpm=_int_or_none(raw.get("rpm")),
        rpd=_int_or_none(raw.get("rpd")),
        tpm=_int_or_none(raw.get("tpm")),
        tpd=_int_or_none(raw.get("tpd")),
    )


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
        rate_limit=_rate_limit_from_entry(entry.get("rate_limit")),
    )


def key_info_from_entry(ref: str, raw: object) -> KeyInfo:
    """Parse one ``[keys.REF]`` entry; a bare string is the legacy help-only form."""
    if isinstance(raw, str):
        return KeyInfo(api_key_ref=ref, effort=None, value=None, help=raw)
    if not isinstance(raw, dict):
        return KeyInfo(api_key_ref=ref, effort=None, value=None, help="")
    try:
        effort = EffortLevel(raw["effort"]) if "effort" in raw else None
    except ValueError:
        effort = None
    try:
        value = ValueLevel(raw["value"]) if "value" in raw else None
    except ValueError:
        value = None
    help_text = raw.get("help")
    return KeyInfo(
        api_key_ref=ref,
        effort=effort,
        value=value,
        help=help_text if isinstance(help_text, str) else "",
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


def _scope_key(user_id: int | str | None) -> str:
    """JSON-object key for one user's profile scope; ``""`` for the unscoped default.

    Safe because ``check_user_id`` forbids a real ``user_id`` from being an empty string.
    """
    return "" if user_id is None else str(user_id)


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.remove(tmp_name)
        raise


class Registry:
    """File-backed read-only registry — ``.toml`` / ``.json`` by extension.

    The catalog config itself stays read-only, but the learned profile is
    persisted to a sibling JSON file (``<config_stem>.profile.json`` by
    default) so a preset overwriting the config file cannot touch learned
    data. ``persist_profile=False`` is the explicit zero-write mode: profiles
    live only in process memory for the caller's lifetime.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        profile_path: str | Path | None = None,
        persist_profile: bool = True,
    ) -> None:
        self._path = Path(path)
        self._persist_profile = persist_profile
        self._profile_path = (
            Path(profile_path)
            if profile_path is not None
            else self._path.parent / f"{self._path.stem}.profile.json"
        )

    async def load(self, user_id: int | str | None = None) -> list[LLMConfig]:  # noqa: ARG002
        data = _read_data(self._path)
        result: list[LLMConfig] = []
        for entry in data.get("llms", []):
            cfg = _config_from_entry(entry)
            if cfg is not None:
                result.append(cfg)
        return result

    async def key_info(self) -> dict[str, KeyInfo]:
        """Per-provider onboarding metadata from the ``[keys]`` table, keyed by ``api_key_ref``."""
        raw = _read_data(self._path).get("keys", {})
        if not isinstance(raw, dict):
            return {}
        return {str(ref): key_info_from_entry(str(ref), val) for ref, val in raw.items()}

    def _read_profile_file(self) -> dict:
        if not self._profile_path.exists():
            return {}
        return json.loads(self._profile_path.read_text(encoding="utf-8"))

    async def read_profiles(self, user_id: int | str | None = None) -> dict[str, LLMProfile]:
        check_user_id(user_id)
        if not self._persist_profile:
            return {}
        scope = self._read_profile_file().get(_scope_key(user_id), {})
        if not isinstance(scope, dict):
            return {}
        return {name: LLMProfile.from_dict(d) for name, d in scope.items()}

    async def write_profile(
        self,
        name: str,
        profile: LLMProfile,
        user_id: int | str | None = None,
    ) -> None:
        check_user_id(user_id)
        if not self._persist_profile:
            return
        data = self._read_profile_file()
        scope = data.setdefault(_scope_key(user_id), {})
        scope[name] = profile.to_dict()
        _atomic_write_json(self._profile_path, data)

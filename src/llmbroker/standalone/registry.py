"""File-backed registry: a ``.toml`` file of ``[[llms]]`` rows, no secrets."""

import tomllib
from pathlib import Path

from llmbroker.models import KeyInfo, Lineup, LLMConfig, check_weight


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


def config_from_entry(entry: dict) -> LLMConfig | None:
    name = entry.get("name")
    base_url = entry.get("base_url")
    if not name or not base_url:
        return None
    return LLMConfig(
        name=str(name),
        base_url=str(base_url),
        model=str(entry.get("model", "")),
        api_key_ref=str(entry.get("api_key_ref", "")),
        parallel=_int_or_none(entry.get("parallel")),
        # The file is llmbroker's own output, so everything in it came from a preset.
        from_preset=True,
        weight=_weight_from_entry(entry.get("weight"), name),
    )


def _check_unique_names(configs: list[LLMConfig]) -> None:
    seen: set[str] = set()
    for cfg in configs:
        if cfg.name in seen:
            raise ValueError(
                f"Registry: duplicate name {cfg.name!r} — a name identifies exactly one entry",
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


def _key_infos(data: dict) -> dict[str, KeyInfo]:
    raw = data.get("keys", {})
    if not isinstance(raw, dict):
        # ValueError, not TypeError: see the entry check in parse_lineup.
        raise ValueError(  # noqa: TRY004
            f"Registry: [keys] is {type(raw).__name__}, not a table of api_key_ref"
            " sections — this is where a human is told how to obtain each key",
        )
    return {str(ref): key_info_from_entry(str(ref), val) for ref, val in raw.items()}


def _check_no_declared_entries(data: dict) -> None:
    """Refused rather than dropped: silently ignoring the section a previous release
    wrote would take models out of an installation without saying so."""
    if data.get("custom"):
        raise ValueError(
            "Registry: the model list carries [[custom]] entries — a model reached by"
            " name is declared in code with direct=[...], never stored. Remove the"
            " section and declare those models where the broker is built",
        )


def parse_lineup(data: dict) -> Lineup:
    """The one reader of a model list: the ``[[llms]]`` entries in file order plus the
    ``[keys]`` metadata. Whether a list is valid is decided only here."""
    _check_no_declared_entries(data)
    configs: list[LLMConfig] = []
    for position, entry in enumerate(data.get("llms", []), start=1):
        if not isinstance(entry, dict):
            # ValueError, not TypeError: a malformed lineup must stay inside the
            # error type a background refresh catches rather than kill the process.
            raise ValueError(  # noqa: TRY004
                f"Registry: [[llms]] entry {position} is {type(entry).__name__}, not a table",
            )
        cfg = config_from_entry(entry)
        if cfg is not None:
            configs.append(cfg)
    _check_unique_names(configs)
    return Lineup(configs=configs, keys=_key_infos(data))


def _read_data(path: Path) -> dict:
    """Parse the file into a dict; ``{}`` if missing."""
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def read_lineup(path: Path) -> Lineup:
    """The lineup this installation follows: its entries and its ``[keys]`` help."""
    return parse_lineup(_read_data(path))


class Registry:
    """File-backed read-only registry over a TOML lineup.

    The file is llmbroker's own output, rewritten in full by a sync; see
    ``specs/reference/rules/sync-merge.md``.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    async def load(self) -> list[LLMConfig]:
        """The ``[[llms]]`` entries of the file, validated."""
        return read_lineup(self._path).configs

    async def key_info(self) -> dict[str, KeyInfo]:
        """Per-provider onboarding metadata from the ``[keys]`` table, keyed by ``api_key_ref``."""
        return read_lineup(self._path).keys

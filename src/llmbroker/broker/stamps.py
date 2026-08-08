"""When this installation last looked upstream, kept across process exits.

Nothing here is authoritative — a stamp only makes checks less frequent — so
every failure degrades to "no stamp" instead of surfacing.
"""

import json
import logging
import time
from pathlib import Path

from llmbroker.util.atomic import write_atomic

logger = logging.getLogger("llmbroker.broker")

_STAMP_FILE = "sync-stamps.json"


def _stamp_path(home: Path) -> Path:
    return home / _STAMP_FILE


def _load(home: Path) -> dict:
    try:
        data = json.loads(_stamp_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def stamp_age(home: Path | None, key: str) -> float | None:
    """Seconds since this check last completed, ``None`` when there is no usable
    record. A timestamp in the future — a clock moved back, a stamp copied between
    hosts — counts as absent rather than as a gate that never expires."""
    if home is None:
        return None
    entry = _load(home).get(key)
    if not isinstance(entry, dict):
        return None
    checked_at = entry.get("checked_at")
    if not isinstance(checked_at, (int, float)) or isinstance(checked_at, bool):
        return None
    age = time.time() - float(checked_at)
    return age if age >= 0 else None


def write_stamp(home: Path | None, key: str) -> None:
    """Record that this check just completed — wall clock, because the whole point
    is to survive process exit."""
    if home is None:
        return
    data = _load(home)
    data[key] = {"checked_at": time.time()}
    try:
        write_atomic(_stamp_path(home), json.dumps(data, indent=2, sort_keys=True) + "\n")
    except OSError as exc:
        logger.debug("sync stamp: cannot write %s (%s)", _stamp_path(home), exc)

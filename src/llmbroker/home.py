"""The one directory llmbroker keeps its own cached state in.

Nothing here is authoritative: a preset can be re-fetched, a stamp only makes a
check less frequent. So no step may raise — every candidate that cannot be
written falls through to the next, and nowhere writable is a supported outcome.
"""

import contextlib
import os
import sys
import tempfile
from pathlib import Path

HOME_ENV_VAR = "LLMBROKER_HOME"
_APP = "llmbroker"

# Probing is a mkdir plus a real write: os.access lies on network filesystems and
# under containers, and the answer is stable for the life of a process.
_probed: dict[Path, bool] = {}


def _platform_cache_dir() -> Path | None:
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg) / _APP
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        return Path(local) / _APP if local else None
    try:
        base = Path.home()
    except (OSError, RuntimeError):
        return None
    if sys.platform == "darwin":
        return base / "Library" / "Caches" / _APP
    return base / ".cache" / _APP


def _temp_dir() -> Path:
    """A per-user subdirectory of the system temp — the last resort before nothing.
    Per-user because a shared /tmp with another user's directory in it is not ours
    to write into."""
    try:
        who = str(os.getuid())  # type: ignore[attr-defined]
    except AttributeError:
        who = os.environ.get("USERNAME") or "user"
    return Path(tempfile.gettempdir()) / f"{_APP}-{who}"


def _candidates(override: str | Path | None) -> list[Path]:
    found = [Path(override).expanduser()] if override is not None else []
    env = os.environ.get(HOME_ENV_VAR)
    if env:
        found.append(Path(env).expanduser())
    platform_dir = _platform_cache_dir()
    if platform_dir is not None:
        found.append(platform_dir)
    found.append(_temp_dir())
    return found


def _is_writable(path: Path) -> bool:
    cached = _probed.get(path)
    if cached is not None:
        return cached
    probe = path / f".probe-{os.getpid()}"
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe.write_text("", encoding="utf-8")
    except OSError:
        _probed[path] = False
        return False
    finally:
        with contextlib.suppress(OSError):
            probe.unlink(missing_ok=True)
    _probed[path] = True
    return True


def home_dir(override: str | Path | None = None) -> Path | None:
    """Where llmbroker keeps what it caches on its own, first writable candidate
    winning: ``override``, ``$LLMBROKER_HOME``, the platform cache directory, a
    per-user temp directory. ``None`` when nowhere is writable — every caller
    degrades to memory rather than failing."""
    for candidate in _candidates(override):
        if _is_writable(candidate):
            return candidate
    return None

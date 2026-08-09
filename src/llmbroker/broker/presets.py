"""Where a curated lineup text comes from: the network, this machine's cache, the wheel.

Every network read in the library goes through here. See
``specs/reference/rules/presets.md`` for the precedence and why it is that way.
"""

import logging
import re
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from http.client import HTTPException
from importlib import resources
from pathlib import Path

from llmbroker.util.atomic import write_atomic

logger = logging.getLogger("llmbroker.broker")

PRESET_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
PAID_CATALOG = "paid-catalog"
_PRESET_URL = (
    "https://raw.githubusercontent.com/andgineer/llmbroker/main/src/llmbroker/presets/{name}.toml"
)
_FETCH_TIMEOUT = 10


def fetch_preset_text(name: str) -> str:
    """Download ``presets/<name>.toml`` from the catalog.

    Every failure is a ``ValueError`` carrying an admin-readable message: nothing
    has been touched yet, so the sync simply does not happen.
    """
    if not PRESET_NAME_RE.match(name):
        raise ValueError(
            f"invalid preset name '{name}' (use letters, digits, hyphens, underscores)",
        )
    url = _PRESET_URL.format(name=name)
    # The read and the decode share the connect phase's try: a body that dies
    # mid-stream raises TimeoutError/IncompleteRead, and a caller that treats a
    # fetch failure as best-effort catches ValueError only.
    try:
        with urllib.request.urlopen(url, timeout=_FETCH_TIMEOUT) as resp:  # noqa: S310 - validated
            text = resp.read().decode()
    except urllib.error.HTTPError as exc:
        if exc.code == HTTPStatus.NOT_FOUND:
            raise ValueError(f"preset '{name}' not found in catalog") from exc
        raise ValueError(f"HTTP {exc.code} fetching {url}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(str(exc.reason)) from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"downloaded content for '{name}' is not valid UTF-8") from exc
    except (OSError, HTTPException) as exc:
        raise ValueError(f"failed reading {url}: {exc!r}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"downloaded content for '{name}' is not valid TOML") from exc
    _check_fetched_urls(name, data)
    _check_no_custom(name, data)
    return text


def _check_no_custom(name: str, data: dict) -> None:
    """``[[custom]]`` means *the host's own*, so a curated lineup declaring one is a
    contradiction — and it would arrive as an entry the pool never routes and no sync
    ever removes. Refused whole, like a plaintext base_url."""
    if data.get("custom"):
        raise ValueError(
            f"preset '{name}' carries [[custom]] entries — a curated lineup states the"
            " pool only, and your own models are yours to declare — refusing the whole file",
        )


def _check_fetched_urls(name: str, data: dict) -> None:
    """A fetched lineup decides where this installation's API keys are sent, so the
    whole file is refused before any merge sees it. Not a defence against a
    compromised catalog — that one would serve ``https://`` too — it removes
    plaintext key transmission as an accident."""
    for section in ("llms", "custom", "provider"):
        for entry in data.get(section, []):
            url = entry.get("base_url") if isinstance(entry, dict) else None
            if url is not None and not str(url).startswith("https://"):
                raise ValueError(
                    f"preset '{name}' carries a non-https base_url ({url})"
                    " — refusing the whole file",
                )


def bundled_preset_text(name: str) -> str | None:
    """The copy shipped in the wheel — the floor under the chain, so a first run with
    no network and a cold cache still has a lineup to start from."""
    resource = resources.files("llmbroker").joinpath("presets", f"{name}.toml")
    try:
        return resource.read_text(encoding="utf-8")
    except (OSError, ModuleNotFoundError):
        return None


@dataclass(frozen=True, slots=True)
class PresetSource:
    """The three copies of a curated text, and one precedence over them.

    ``cache_dir`` is this installation's home; ``None`` means nowhere is writable,
    and then only the network and the wheel are left.
    """

    cache_dir: Path | None = None

    def text(self, name: str, *, prefer_cache: bool = False, floor: bool = True) -> str:
        """A curated text, by the precedence the caller's decision needs.

        The default is a fetch with the copies already here behind it: a successful
        fetch overwrites the cache, a failed one — offline, or throttled by the CDN's
        per-IP limit — reads it, which is what keeps such an installation running on
        what it last saw.

        ``prefer_cache`` reads the cache first, for a resolution on the request path
        that must not wait on the network. ``floor=False`` drops the copy bundled in
        the wheel, for a read that decides where an entry someone *already has*
        should point: that copy is older than the repository by construction, so
        serving it there would roll a working model backwards.
        """
        if prefer_cache:
            cached = self._cached(name)
            if cached is not None:
                return cached
        try:
            text = fetch_preset_text(name)
        except ValueError as exc:
            cached = self._cached(name)
            if cached is not None:
                logger.debug("preset %s: %s — falling back to the cached copy", name, exc)
                return cached
            bundled = bundled_preset_text(name) if floor else None
            if bundled is None:
                raise
            # Louder than the cached fallback, which serves what this machine last
            # saw: the wheel's copy is frozen at the installed release, and what it
            # seeds a lineup with is what the installation runs until the next one.
            logger.warning(
                "preset %s: %s — falling back to the bundled copy, frozen at this"
                " llmbroker release and possibly older than the curated one",
                name,
                exc,
            )
            return bundled
        self._write_cache(name, text)
        return text

    def refresh(self, name: str) -> None:
        """Pull a fresh copy into the cache. Raises when the fetch fails — the caller
        decides whether the copy already here is good enough."""
        self._write_cache(name, fetch_preset_text(name))

    def _path(self, name: str) -> Path | None:
        return None if self.cache_dir is None else self.cache_dir / "presets" / f"{name}.toml"

    def _cached(self, name: str) -> str | None:
        path = self._path(name)
        if path is None:
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def _write_cache(self, name: str, text: str) -> None:
        path = self._path(name)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(path, text)
        except OSError as exc:
            logger.debug("preset %s: cannot cache into %s (%s)", name, path, exc)

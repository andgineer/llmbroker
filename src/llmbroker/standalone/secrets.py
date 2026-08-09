"""Read-only secrets resolvers with no external backend: ``Secrets`` over
``os.environ`` with an optional ``.env`` behind it, ``DictSecrets`` over a mapping.
A plain callable is accepted and adapted."""

import inspect
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

from llmbroker.protocols.secrets import SecretsProtocol


def parse_env_file(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines, skipping blanks, ``#`` comments, and anything
    else — a malformed line is not worth failing a whole deployment over.

    >>> parse_env_file('# a comment\\nA=1\\nexport B="two"\\nnonsense\\n')
    {'A': '1', 'B': 'two'}
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip().removeprefix("export ").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):  # noqa: PLR2004
            value = value[1:-1]
        else:
            # An unquoted value ends at an inline comment; a key silently carrying
            # a trailing "# note" would be rejected as a dead one by the provider.
            value = value.partition(" #")[0].rstrip()
        values[key] = value
    return values


class Secrets:
    """Read-only env-backed secrets resolver (the default battery). ``env_file`` is
    consulted only where the real environment has no such variable, and a blank value
    counts as absent either way."""

    def __init__(self, env_file: str | Path | None = None) -> None:
        self._env_file = Path(env_file) if env_file is not None else None
        self._file_values: dict[str, str] | None = None
        self._file_stamp: tuple[int, int] | None = None

    def _file_mapping(self, path: Path) -> dict[str, str]:
        """Re-parse whenever the file changes, so a key filled in on a running
        broker takes effect on the next resync exactly as an exported one would."""
        try:
            info = path.stat()
            stamp = (info.st_mtime_ns, info.st_size)
        except OSError:
            stamp = None
        if self._file_values is None or stamp != self._file_stamp:
            self._file_stamp = stamp
            try:
                self._file_values = parse_env_file(path.read_text(encoding="utf-8"))
            except OSError:
                self._file_values = {}
        return self._file_values

    def _from_file(self, ref: str) -> str | None:
        if self._env_file is None:
            return None
        # The skeleton `llmbroker env` writes is all `KEY=` lines: an unfilled one
        # must leave the model keyless, not hand the provider an empty credential.
        return self._file_mapping(self._env_file).get(ref) or None

    async def resolve(self, ref: str) -> str:
        # A blank export is as unset as no export at all — key presence authorizes
        # removals in a preset sync, so it must never be satisfied by whitespace.
        value = os.environ.get(ref)
        if value is None or not value.strip():
            value = self._from_file(ref)
        if value is None or not value.strip():
            raise KeyError(f"Secrets: env var {ref!r} is not set")
        return value


class DictSecrets:
    """Read-only secrets resolver backed by an in-memory mapping (tests / preloaded keys)."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = dict(mapping)

    async def resolve(self, ref: str) -> str:
        if ref not in self._mapping:
            raise KeyError(f"DictSecrets: ref {ref!r} not found")
        return self._mapping[ref]


class _CallableSecrets:
    """Adapter wrapping a ``Callable[[str], str | Awaitable[str]]`` as a SecretsProtocol."""

    def __init__(self, fn: Callable[[str], str | Awaitable[str]]) -> None:
        self._fn = fn

    async def resolve(self, ref: str) -> str:
        result = self._fn(ref)
        if inspect.isawaitable(result):
            return str(await result)
        return str(result)


def as_secrets(secrets: object) -> SecretsProtocol:
    """Return a SecretsProtocol, wrapping a bare callable if needed."""
    if secrets is None:
        return Secrets()
    if isinstance(secrets, SecretsProtocol):
        return secrets
    if callable(secrets):
        return _CallableSecrets(cast(Callable[[str], str | Awaitable[str]], secrets))
    raise TypeError(f"secrets must be a SecretsProtocol or callable, got {type(secrets)!r}")

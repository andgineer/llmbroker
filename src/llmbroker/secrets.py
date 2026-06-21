"""Secrets port protocols and the zero-dependency batteries.

``Secrets()`` resolves ``api_key_ref`` from ``os.environ``; ``DictSecrets``
from a mapping. Both are read-only — ``.set()`` raises
``SecretsReadOnlyError``. A plain callable is accepted and adapted.
"""

import inspect
import os
from collections.abc import Awaitable, Callable
from typing import Protocol, cast, runtime_checkable


class SecretsReadOnlyError(Exception):
    """Raised when ``.set()`` is called on a read-only secrets battery."""


class UserScopeError(Exception):
    """Raised when ``user_id`` is ``None`` and ``require_user_id=True``."""


@runtime_checkable
class SecretsProtocol(Protocol):
    async def resolve(self, ref: str, user_id: int | str | None = None) -> str: ...


@runtime_checkable
class MutableSecretsProtocol(SecretsProtocol, Protocol):
    async def set(self, ref: str, value: str, user_id: int | str | None = None) -> None: ...


class Secrets:
    """Read-only env-backed secrets resolver (the default battery)."""

    def __init__(self, *, require_user_id: bool = False) -> None:
        self._require_user_id = require_user_id

    async def resolve(self, ref: str, user_id: int | str | None = None) -> str:
        if self._require_user_id and user_id is None:
            raise UserScopeError(
                "Secrets: user_id is required (require_user_id=True) but received None",
            )
        value = os.environ.get(ref)
        if value is None:
            raise KeyError(f"Secrets: env var {ref!r} is not set")
        return value


class DictSecrets:
    """Read-only secrets resolver backed by an in-memory mapping (tests / preloaded keys)."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = dict(mapping)

    async def resolve(self, ref: str, user_id: int | str | None = None) -> str:  # noqa: ARG002
        if ref not in self._mapping:
            raise KeyError(f"DictSecrets: ref {ref!r} not found")
        return self._mapping[ref]


class _CallableSecrets:
    """Adapter wrapping a ``Callable[[str], str | Awaitable[str]]`` as a SecretsProtocol."""

    def __init__(self, fn: Callable[[str], str | Awaitable[str]]) -> None:
        self._fn = fn

    async def resolve(self, ref: str, user_id: int | str | None = None) -> str:  # noqa: ARG002
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

"""Env-var and in-memory secrets resolvers — read-only, no external backend.

``Secrets()`` resolves ``api_key_ref`` from ``os.environ``; ``DictSecrets``
from a mapping. Both are read-only. A plain callable is accepted and adapted.
"""

import inspect
import os
from collections.abc import Awaitable, Callable
from typing import cast

from llmbroker.exceptions import UserScopeError
from llmbroker.protocols.secrets import SecretsProtocol


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

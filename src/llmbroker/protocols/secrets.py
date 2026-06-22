"""Secrets contract: resolve ``api_key_ref`` to a key; mutable backends also set."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class SecretsProtocol(Protocol):
    async def resolve(self, ref: str, user_id: int | str | None = None) -> str: ...


@runtime_checkable
class MutableSecretsProtocol(SecretsProtocol, Protocol):
    async def set(self, ref: str, value: str, user_id: int | str | None = None) -> None: ...

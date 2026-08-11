"""Secrets contract: resolve ``api_key_ref`` to a key; mutable backends also set,
and a backend that can list its own refs answers the pool's key question in one
call instead of one per ref."""

from typing import Protocol, runtime_checkable


@runtime_checkable
class SecretsProtocol(Protocol):
    async def resolve(self, ref: str) -> str: ...


@runtime_checkable
class MutableSecretsProtocol(SecretsProtocol, Protocol):
    async def set(self, ref: str, value: str) -> None: ...


@runtime_checkable
class EnumerableSecretsProtocol(SecretsProtocol, Protocol):
    """Optional: the refs this store holds under a prefix, the prefix included.

    A backend without it is asked ref by ref, which is what an environment-backed
    store wants — the lookup is free there and there is nothing to enumerate.
    """

    async def refs(self, prefix: str = "") -> frozenset[str]: ...
